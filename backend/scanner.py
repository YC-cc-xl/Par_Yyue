"""分集扫描模块

规则：选中根目录后，只遍历【一级子文件夹】，一个子文件夹 = 一个分集；
每个分集目录内分为【视频】【音频】两个子文件夹，按「去掉扩展名后基础文件名完全相同」配对：

    视频\\xxx.mp4  +  音频\\xxx.mp3  +  音频\\xxx.srt   →  一组有效素材

缺少 mp4 / mp3 / srt 中任意一项都不算有效组：不统计、不渲染，
并记录原因（缺哪个文件）供任务日志提示。
（该模块被 FastAPI 接口层与并发渲染层共用，避免逻辑重复）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ffmpeg_render import probe_sources

# 识别用的扩展名（统一小写比较）
VIDEO_EXTS = (".mp4",)
AUDIO_EXTS = (".mp3",)
SUBTITLE_EXTS = (".srt",)

# 分集内的素材子文件夹名（素材必须放在这两个文件夹里才会被识别）
VIDEO_DIR_NAME = "视频"
AUDIO_DIR_NAME = "音频"

# 输出目录名（由 task_runner 创建在「处理来源」根目录下）。
# 扫描时必须跳过，否则对同一根目录二次扫描会把输出目录误当成一个分集，
# 进而在后续合成中多出一条「缺少配音」的失败记录。
OUTPUT_DIR_NAME = "自动合成输出"


def _files_in(dir_path: Path, exts: tuple[str, ...]) -> list[Path]:
    """列出素材子文件夹内的文件（忽略隐藏文件；先找直属文件，找不到再递归）

    返回结果按文件名升序，保证多次扫描顺序稳定可复现。
    """
    if not dir_path.is_dir():
        return []

    def matches(base: Path) -> list[Path]:
        return [
            p
            for p in sorted(base.iterdir(), key=lambda item: item.name.lower())
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in exts
        ]

    files = matches(dir_path)
    if files:
        return files
    return [
        p
        for p in sorted(dir_path.rglob("*"), key=lambda item: item.name.lower())
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in exts
    ]


def _index_by_stem(files: list[Path]) -> dict[str, Path]:
    """按基础文件名（去扩展名、小写）建立索引，同名文件以先出现的为准"""
    indexed: dict[str, Path] = {}
    for path in files:
        indexed.setdefault(path.stem.lower(), path)
    return indexed


def scan_episode(sub_dir: Path) -> dict[str, Any]:
    """扫描单个分集目录，返回有效素材组与被跳过的素材

    有效组判定：视频\\xxx.mp4 + 音频\\xxx.mp3 + 音频\\xxx.srt 三者基础文件名完全相同。
    """
    videos = _files_in(sub_dir / VIDEO_DIR_NAME, VIDEO_EXTS)
    audios = _files_in(sub_dir / AUDIO_DIR_NAME, AUDIO_EXTS)
    subtitles = _files_in(sub_dir / AUDIO_DIR_NAME, SUBTITLE_EXTS)

    audio_by_stem = _index_by_stem(audios)
    subtitle_by_stem = _index_by_stem(subtitles)

    groups: list[dict[str, Any]] = []
    skipped: list[str] = []

    # ---- 以视频文件为入口，逐个检查同名配音与字幕 ----
    for video in videos:
        stem = video.stem
        audio = audio_by_stem.get(stem.lower())
        subtitle = subtitle_by_stem.get(stem.lower())
        lacking = []
        if audio is None:
            lacking.append(f"{AUDIO_DIR_NAME}\\{stem}.mp3")
        if subtitle is None:
            lacking.append(f"{AUDIO_DIR_NAME}\\{stem}.srt")
        if lacking:
            skipped.append(f"{VIDEO_DIR_NAME}\\{video.name} 缺少 {'、'.join(lacking)}")
            continue

        groups.append(
            {
                "name": stem,
                "videoPath": str(video),
                "audioPath": str(audio),
                "subtitlePath": str(subtitle),
            }
        )

    # ---- 音频文件夹里多出来的素材（没有同名视频，或本身没配成对） ----
    matched_stems = {group["name"].lower() for group in groups}
    video_stems = {video.stem.lower() for video in videos}
    for audio in audios:
        stem = audio.stem
        key = stem.lower()
        if key in matched_stems:
            continue
        # 只报该 mp3 自身缺的配对项：没有同名 srt 记字幕，没有同名 mp4 才记视频
        lacking = []
        if key not in subtitle_by_stem:
            lacking.append(f"{AUDIO_DIR_NAME}\\{stem}.srt")
        if key not in video_stems:
            lacking.append(f"{VIDEO_DIR_NAME}\\{stem}.mp4")
        if lacking:
            skipped.append(f"{AUDIO_DIR_NAME}\\{audio.name} 缺少 {'、'.join(lacking)}")

    for subtitle in subtitles:
        stem = subtitle.stem
        key = stem.lower()
        if key in matched_stems or key in audio_by_stem:
            continue
        lacking = [f"{AUDIO_DIR_NAME}\\{stem}.mp3"]
        if key not in video_stems:
            lacking.append(f"{VIDEO_DIR_NAME}\\{stem}.mp4")
        skipped.append(f"{AUDIO_DIR_NAME}\\{subtitle.name} 缺少 {'、'.join(lacking)}")

    return {
        "groups": groups,
        "skipped": skipped,
        "videoCount": len(videos),
    }


def scan_episodes(root_dir: str | Path) -> dict[str, Any]:
    """扫描根目录，返回分集清单

    返回结构：
      { "root": str, "total": int, "episodes": [ 分集条目 ] }
    分集条目字段：
      index/name/dirPath                分集序号、目录名、目录绝对路径
      groups                            有效素材组列表（一组 = mp4+mp3+srt 同名）
      skipped                           被跳过的素材及缺失原因
      videoCount                        该分集内 mp4 素材数量
      videoPath/audioPath/subtitlePath  兼容字段：取第一组有效素材，无有效组时为空
      missing                           兼容字段：无有效组时为 ["视频", "配音"]
    """
    root = Path(root_dir).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"根目录不存在：{root}")
    if not root.is_dir():
        raise NotADirectoryError(f"所选路径不是文件夹：{root}")

    # 只取一级子文件夹作为分集，按名称排序保证顺序稳定；跳过输出目录
    sub_dirs = sorted(
        (
            p
            for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name != OUTPUT_DIR_NAME
        ),
        key=lambda p: p.name.lower(),
    )

    episodes: list[dict[str, Any]] = []
    for index, sub_dir in enumerate(sub_dirs, start=1):
        scanned = scan_episode(sub_dir)
        groups = scanned["groups"]
        first = groups[0] if groups else None

        episodes.append(
            {
                "index": index,
                "name": sub_dir.name,
                "dirPath": str(sub_dir),
                "groups": groups,
                "skipped": scanned["skipped"],
                "videoCount": scanned["videoCount"],
                # 兼容原有前端/渲染层字段：取第一组有效素材
                "videoPath": first["videoPath"] if first else "",
                "audioPath": first["audioPath"] if first else "",
                "subtitlePath": first["subtitlePath"] if first else "",
                "missing": [] if first else ["视频", "配音"],
            }
        )

    # 扫描阶段一次性预解析全部源素材（分辨率/编码/采样率/通道）写入内存缓存，
    # 后续构建 ffmpeg 命令直接读缓存，避免每个 ffmpeg 子进程重复探测源文件
    sources = [
        path
        for episode in episodes
        for group in episode["groups"]
        for path in (group.get("videoPath"), group.get("audioPath"))
        if path
    ]
    try:
        probe_sources(sources)
    except Exception:  # 探测失败不影响扫描结果，仅失去缓存加速
        pass

    return {"root": str(root), "total": len(episodes), "episodes": episodes}

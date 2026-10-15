"""分集扫描模块（三层目录）

目录层级（本次改造后）：
    一级：项目根目录（可批量选择多个）
      └─ 二级：分集文件夹（一个分集目录 = 一个分集）
           ├─ 三级：【视频】文件夹（放 .mp4）
           ├─ 三级：【音频】文件夹（放同名 .mp3 与同名 .srt）
           └─ 【处理完成】文件夹（渲染后自动创建，成品 mp4 落在这里）

规则要点：
  1. 二级分集内必须同时存在【视频】【音频】两个三级文件夹，否则整集跳过并告警
  2. 素材配对【只在同一个二级分集内部】进行，跨二级分集、跨一级项目一律禁止
  3. 配对：视频\\xxx.mp4 + 音频\\xxx.mp3 + 音频\\xxx.srt 三者主文件名完全一致 → 一组任务；
     缺任一项记入 skipped 告警，跳过本组
  4. 断点续跑：成品（分集\\处理完成\\xxx.mp4）已存在 → 该组标记 resumed，不重复渲染

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

# 成品输出目录名：创建在【二级分集文件夹】内（渲染前由调度层创建）
DONE_DIR_NAME = "处理完成"

# 旧版根级输出目录名。扫描一级项目目录时仍需跳过，避免历史遗留目录被当成一个分集
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


def _list_sub_dirs(root: Path) -> list[Path]:
    """列出项目根目录下的二级分集文件夹（忽略隐藏目录与旧版输出目录），按名称排序"""
    return sorted(
        (
            p
            for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name != OUTPUT_DIR_NAME
        ),
        key=lambda p: p.name.lower(),
    )


def _has_material_dirs(sub_dir: Path) -> bool:
    """二级分集内必须同时存在【视频】【音频】两个三级文件夹才算有效分集"""
    return (sub_dir / VIDEO_DIR_NAME).is_dir() and (sub_dir / AUDIO_DIR_NAME).is_dir()


def scan_project(root_dir: str | Path, index: int = 1) -> dict[str, Any]:
    """扫描单个一级项目根目录

    返回：
      index/name/root                     项目序号、目录名、绝对路径
      episodes                            分集列表（见下）
      warnings                            项目级告警（如某个分集缺三级文件夹）
      taskTotal/taskPending/taskResumed   该项目素材组总数 / 待渲染数 / 已存在跳过数

    分集条目字段：
      index/name/dirPath                  分集序号、目录名、目录绝对路径
      groups                              有效素材组（含 outputPath 与 resumed 标记）
      skipped                             被跳过的素材及缺失原因
      videoCount/pendingCount/resumedCount 素材数量与待渲染/续跑数量
    """
    root = Path(root_dir).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"项目根目录不存在：{root}")
    if not root.is_dir():
        raise NotADirectoryError(f"所选路径不是文件夹：{root}")

    episodes: list[dict[str, Any]] = []
    warnings: list[str] = []

    for episode_index, sub_dir in enumerate(_list_sub_dirs(root), start=1):
        # 三级文件夹校验：缺少【视频】或【音频】任一 → 整集跳过并告警
        if not _has_material_dirs(sub_dir):
            warnings.append(
                f"{sub_dir.name}：缺少【{VIDEO_DIR_NAME}】或【{AUDIO_DIR_NAME}】文件夹，整集跳过"
            )
            continue

        scanned = scan_episode(sub_dir)
        groups: list[dict[str, Any]] = []
        for group in scanned["groups"]:
            # 成品固定落在本分集的【处理完成】目录内
            out_file = sub_dir / DONE_DIR_NAME / f"{group['name']}.mp4"
            groups.append(
                {
                    **group,
                    "outputPath": str(out_file),
                    # 断点续跑：成品已存在 → 本组不重复渲染
                    "resumed": out_file.is_file(),
                }
            )

        first = groups[0] if groups else None
        episodes.append(
            {
                "index": episode_index,
                "name": sub_dir.name,
                "dirPath": str(sub_dir),
                "groups": groups,
                "skipped": scanned["skipped"],
                "videoCount": scanned["videoCount"],
                "pendingCount": sum(1 for g in groups if not g["resumed"]),
                "resumedCount": sum(1 for g in groups if g["resumed"]),
                # 兼容字段：取第一组有效素材
                "videoPath": first["videoPath"] if first else "",
                "audioPath": first["audioPath"] if first else "",
                "subtitlePath": first["subtitlePath"] if first else "",
                "missing": [] if first else [VIDEO_DIR_NAME, AUDIO_DIR_NAME],
            }
        )

    # 扫描阶段一次性预解析全部待渲染源素材（分辨率/编码/采样率/通道）写入内存缓存，
    # 后续构建 ffmpeg 命令直接读缓存；已续跑跳过的组无需探测，省掉无谓 IO
    sources = [
        path
        for episode in episodes
        for group in episode["groups"]
        if not group["resumed"]
        for path in (group.get("videoPath"), group.get("audioPath"))
        if path
    ]
    try:
        probe_sources(sources)
    except Exception:  # 探测失败不影响扫描结果，仅失去缓存加速
        pass

    return {
        "index": index,
        "name": root.name,
        "root": str(root),
        "episodes": episodes,
        "warnings": warnings,
        "taskTotal": sum(len(episode["groups"]) for episode in episodes),
        "taskPending": sum(episode["pendingCount"] for episode in episodes),
        "taskResumed": sum(episode["resumedCount"] for episode in episodes),
    }


def scan_projects(project_dirs: list[str | Path]) -> dict[str, Any]:
    """批量扫描多个一级项目根目录

    project_dirs: 一级项目根目录列表（每个元素 = 一个项目）
    返回：
      projects  各项目扫描结果（结构见 scan_project）
      summary   全局汇总：项目数 / 分集数 / 有效分集数 / 素材组总数 / 待渲染数 / 续跑跳过数
    """
    roots = [str(item).strip() for item in (project_dirs or []) if str(item or "").strip()]
    if not roots:
        raise NotADirectoryError("未指定任何项目根目录，请先添加一级项目文件夹")

    projects = [scan_project(root, index=i) for i, root in enumerate(roots, start=1)]
    episodes = [episode for project in projects for episode in project["episodes"]]

    return {
        "projects": projects,
        "summary": {
            "projectTotal": len(projects),
            "episodeTotal": len(episodes),
            "validEpisodeTotal": sum(1 for episode in episodes if episode["groups"]),
            "groupTotal": sum(len(episode["groups"]) for episode in episodes),
            "groupPendingTotal": sum(episode["pendingCount"] for episode in episodes),
            # 断点续跑：已存在成品而跳过的组数
            "groupResumedTotal": sum(episode["resumedCount"] for episode in episodes),
            "videoTotal": sum(episode["videoCount"] for episode in episodes),
        },
    }


def scan_episodes(root_dir: str | Path) -> dict[str, Any]:
    """单项目扫描（保留旧入口，返回 {"root","total","episodes"}）

    内部复用 scan_project，episodes 结构为带 outputPath/resumed 的超集，旧字段全部保留。
    """
    project = scan_project(root_dir)
    return {
        "root": project["root"],
        "total": len(project["episodes"]),
        "episodes": project["episodes"],
    }

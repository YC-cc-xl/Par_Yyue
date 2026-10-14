"""分集扫描模块

规则：选中根目录后，只遍历【一级子文件夹】，一个子文件夹 = 一个分集；
每个分集目录内自动识别匹配：视频 mp4、配音 mp3、字幕 srt。
（该模块被 FastAPI 接口层与并发渲染层共用，避免逻辑重复）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 识别用的扩展名（统一小写比较）
VIDEO_EXTS = (".mp4",)
AUDIO_EXTS = (".mp3",)
SUBTITLE_EXTS = (".srt",)

# 输出目录名（由 task_runner 创建在「处理来源」根目录下）。
# 扫描时必须跳过，否则对同一根目录二次扫描会把输出目录误当成一个分集，
# 进而在后续合成中多出一条「缺少配音」的失败记录。
OUTPUT_DIR_NAME = "自动合成输出"


def _pick_file(episode_dir: Path, exts: tuple[str, ...]) -> Path | None:
    """在分集目录内挑选匹配的文件

    优先级：
      1) 文件名（不含扩展名）与分集目录名一致的文件
      2) 其余同名扩展名文件按文件名升序取第一个
    先只找目录直属文件，找不到再向子目录递归查找。
    """
    candidates = [
        p for p in episode_dir.iterdir() if p.is_file() and p.suffix.lower() in exts
    ]
    if not candidates:
        candidates = [
            p for p in episode_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts
        ]
    if not candidates:
        return None

    dir_stem = episode_dir.name.lower()
    # 目录名同名的优先；其余按名称升序，保证结果稳定可复现
    candidates.sort(key=lambda p: (p.stem.lower() != dir_stem, p.name.lower()))
    return candidates[0]


def scan_episodes(root_dir: str | Path) -> dict[str, Any]:
    """扫描根目录，返回分集清单

    返回结构：
      { "root": str, "total": int, "episodes": [ {index,name,dirPath,videoPath,audioPath,subtitlePath,missing} ] }
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
        video = _pick_file(sub_dir, VIDEO_EXTS)
        audio = _pick_file(sub_dir, AUDIO_EXTS)
        subtitle = _pick_file(sub_dir, SUBTITLE_EXTS)

        # 视频与配音为必需，字幕可选
        missing: list[str] = []
        if video is None:
            missing.append("视频")
        if audio is None:
            missing.append("配音")

        episodes.append(
            {
                "index": index,
                "name": sub_dir.name,
                "dirPath": str(sub_dir),
                "videoPath": str(video) if video else "",
                "audioPath": str(audio) if audio else "",
                "subtitlePath": str(subtitle) if subtitle else "",
                "missing": missing,
            }
        )

    return {"root": str(root), "total": len(episodes), "episodes": episodes}

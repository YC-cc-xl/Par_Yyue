"""批量并发合成任务调度

职责：
  1. 接收任务 ID，在后台线程中编排整批渲染流程（接口立即返回，前端轮询进度）
  2. 用 concurrent.futures.ThreadPoolExecutor 控制并发，最大并发数取页面【最大任务数】
  3. 逐集调用 ffmpeg_render.render_episode，实时更新成功/失败/进度/日志
  4. 按页面选项生成「合并视频」

关于两个性能参数（页面语义）：
  - 最大任务数 / 启用并行处理：控制同时渲染的分集数量（线程池 worker 数）
  - 性能上限(%)：控制【单个 ffmpeg 进程】可用的编码线程数 = CPU核数 × 上限比例
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import models
from ffmpeg_render import render_episode, concat_videos, locate_ffmpeg, FfmpegNotFound
from scanner import scan_episodes, OUTPUT_DIR_NAME

# 输出目录（相对处理来源根目录）
MERGE_FILE_NAME = "合并视频.mp4"

# 任务日志中单集 ffmpeg 输出的保留行数
LOG_TAIL_LINES = 12


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def start_task_async(task_id: int) -> None:
    """异步启动任务：立即返回，实际渲染在后台守护线程中执行

    由 FastAPI 的 POST /api/compose/tasks 调用。
    """
    thread = threading.Thread(
        target=_run_task,
        args=(task_id,),
        name=f"compose-task-{task_id}",
        daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------------------
# 并发参数解析
# ---------------------------------------------------------------------------

def _cpu_count() -> int:
    return os.cpu_count() or 4


def _resolve_workers(config: dict[str, Any]) -> int:
    """线程池并发数：启用并行处理时取最大任务数，否则串行（1）"""
    performance = config.get("performance") or {}
    if not performance.get("parallelEnabled", True):
        return 1
    try:
        workers = int(performance.get("maxWorkers") or 4)
    except (TypeError, ValueError):
        workers = 4
    return max(1, min(workers, 16))


def _resolve_threads(config: dict[str, Any]) -> int:
    """单个 ffmpeg 进程的编码线程数：CPU核数 × 性能上限比例"""
    performance = config.get("performance") or {}
    try:
        limit = int(performance.get("perfLimit") or 80)
    except (TypeError, ValueError):
        limit = 80
    limit = max(0, min(limit, 100))
    if limit <= 0:
        return 1
    return max(1, min(_cpu_count(), round(_cpu_count() * limit / 100.0)))


# ---------------------------------------------------------------------------
# 任务执行
# ---------------------------------------------------------------------------

def _run_task(task_id: int) -> None:
    """在后台线程中执行整批渲染（含异常兜底，保证任务状态一定能落地）"""
    task = models.get_task(task_id)
    if task is None:
        return

    try:
        _execute(task_id, task)
    except Exception as exc:  # 兜底：任何未预期异常都不能让任务卡在 running
        models.append_task_log(task_id, f"[错误] 任务执行异常：{exc}")
        models.update_task(task_id, status="failed")


def _execute(task_id: int, task: dict[str, Any]) -> None:
    config = models.load_config(task.get("config"))
    source_dir = task["source_dir"]

    # ---- 0. 前置检查：ffmpeg 必须可用 ----
    try:
        ffmpeg_path = locate_ffmpeg()
    except FfmpegNotFound:
        ffmpeg_path = None
    if ffmpeg_path is None:
        models.append_task_log(task_id, "[错误] 未找到 ffmpeg.exe，无法开始渲染")
        models.update_task(task_id, status="failed")
        return

    # ---- 1. 重新扫描，保证与实际文件一致 ----
    scan = scan_episodes(source_dir)
    episodes = scan["episodes"]
    if not episodes:
        models.append_task_log(task_id, "[错误] 未扫描到任何分集")
        models.update_task(task_id, total=0, status="failed")
        return

    output_root = Path(source_dir) / OUTPUT_DIR_NAME
    output_root.mkdir(parents=True, exist_ok=True)
    log_dir = output_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    workers = _resolve_workers(config)
    threads = _resolve_threads(config)
    output_cfg = config.get("output") or {}

    models.update_task(task_id, total=len(episodes), status="running")
    models.append_task_log(task_id, "=" * 60)
    models.append_task_log(task_id, f"[任务] 共 {len(episodes)} 集，并发数 {workers}，每进程编码线程 {threads}")
    models.append_task_log(
        task_id,
        f"[任务] 分辨率 {output_cfg.get('width', 1280)}×{output_cfg.get('height', 720)}，"
        f"码率 {output_cfg.get('bitrate', 2000)}kbps，输出目录：{output_root}",
    )
    models.append_task_log(task_id, "=" * 60)

    # ---- 2. 并发渲染各分集 ----
    lock = threading.Lock()
    stats = {"done": 0, "success": 0, "failed": 0}
    outputs: dict[int, Path] = {}          # 分集序号 → 输出文件
    episode_by_index = {ep["index"]: ep for ep in episodes}

    def render_one(episode: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Path]:
        """渲染单个分集（在线程池中执行）"""
        out_file = output_root / f"{episode['name']}.mp4"

        # 视频/配音缺失的分集直接判定失败，不进入 ffmpeg
        if episode.get("missing"):
            return episode, {
                "success": False,
                "error": f"缺少{'、'.join(episode['missing'])}文件",
                "log": "",
                "command": "",
                "warnings": [],
            }, out_file

        result = render_episode(
            episode,
            out_file,
            config,
            work_sub_name=f"sub_{task_id}_{episode['index']:03d}.ass",
            threads=threads,
        )
        _write_episode_log(log_dir / f"{episode['name']}.log", episode, result)
        return episode, result, out_file

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as pool:
        futures = {pool.submit(render_one, ep): ep for ep in episodes}
        for future in as_completed(futures):
            episode = futures[future]
            label = f"[第{episode['index']}集 {episode['name']}]"
            try:
                episode, result, out_file = future.result()
            except Exception as exc:  # 单集异常不影响其它分集
                episode, result, out_file = episode, {"success": False, "error": f"渲染线程异常：{exc}"}, None

            with lock:
                stats["done"] += 1
                if result.get("success"):
                    stats["success"] += 1
                    outputs[episode["index"]] = out_file
                    models.append_task_log(task_id, f"{label} 成功 → {out_file}")
                    for warning in result.get("warnings") or []:
                        models.append_task_log(task_id, f"{label} 警告：{warning}")
                else:
                    stats["failed"] += 1
                    models.append_task_log(task_id, f"{label} 失败：{result.get('error', '未知错误')}")
                    for line in _tail_lines(result.get("log") or "", LOG_TAIL_LINES):
                        models.append_task_log(task_id, f"{label} | {line}")

                progress = stats["done"] / len(episodes) * 100.0
                models.update_task(
                    task_id,
                    success=stats["success"],
                    failed=stats["failed"],
                    progress=progress,
                )

    # ---- 3. 合并视频 ----
    if output_cfg.get("mergeVideo"):
        _build_merged(task_id, config, output_root, outputs, episode_by_index)

    # ---- 4. 收敛任务状态 ----
    models.update_task(task_id, progress=100.0)
    final_status = "completed" if stats["success"] > 0 else "failed"
    models.update_task(task_id, status=final_status)
    models.append_task_log(
        task_id,
        f"[完成] 成功 {stats['success']} 集，失败 {stats['failed']} 集，共 {len(episodes)} 集",
    )


# ---------------------------------------------------------------------------
# 合并视频
# ---------------------------------------------------------------------------

def _build_merged(
    task_id: int,
    config: dict[str, Any],
    output_root: Path,
    outputs: dict[int, Path],
    episode_by_index: dict[int, dict[str, Any]],
) -> None:
    """按页面选项生成合并视频

    - 创建合并视频：把全部成功分集按序号首尾拼接
    - 合并原视频与第一集：在合并视频最前面拼接【第一集的原始视频】（作为片头）
      （此语义以页面文案直译实现，如与预期不符请告知调整）
    """
    output_cfg = config.get("output") or {}
    ordered = [outputs[index] for index in sorted(outputs)]
    if not ordered:
        models.append_task_log(task_id, "[合并] 没有可合并的成功分集，跳过合并")
        return

    parts: list[Path] = []
    if output_cfg.get("mergeWithFirst"):
        first = episode_by_index.get(min(episode_by_index)) if episode_by_index else None
        first_video = Path(first["videoPath"]) if first and first.get("videoPath") else None
        if first_video and first_video.is_file():
            parts.append(first_video)
            models.append_task_log(task_id, f"[合并] 已加入第一集原视频作为片头：{first_video.name}")
        else:
            models.append_task_log(task_id, "[合并] 未找到第一集原视频，跳过片头")

    parts.extend(ordered)

    merged_file = output_root / MERGE_FILE_NAME
    models.append_task_log(task_id, f"[合并] 开始拼接 {len(parts)} 个片段 → {merged_file}")

    if output_cfg.get("mergeWithFirst") and len(parts) > 1:
        # 片头是原始视频，编码参数与合成结果不同，直接 copy 拼接会失败，故重新编码；
        # 同时按页面配置的分辨率/码率统一重编码，避免成片被片头的原始分辨率带偏
        result = concat_videos(
            parts,
            merged_file,
            reencode=True,
            width=output_cfg.get("width"),
            height=output_cfg.get("height"),
            bitrate=output_cfg.get("bitrate"),
        )
    else:
        result = concat_videos(parts, merged_file)

    if result.get("success"):
        models.append_task_log(task_id, f"[合并] 完成 → {merged_file}")
    else:
        models.append_task_log(task_id, f"[合并] 失败：{result.get('error', '未知错误')}")
        for line in _tail_lines(result.get("log") or "", LOG_TAIL_LINES):
            models.append_task_log(task_id, f"[合并] | {line}")


# ---------------------------------------------------------------------------
# 日志落盘
# ---------------------------------------------------------------------------

def _write_episode_log(log_file: Path, episode: dict[str, Any], result: dict[str, Any]) -> None:
    """把单集的 ffmpeg 完整日志写入文件（满足「捕获全部 stdout/stderr」）"""
    try:
        lines = [
            f"分集：第{episode['index']}集 {episode['name']}",
            f"视频：{episode.get('videoPath')}",
            f"配音：{episode.get('audioPath')}",
            f"字幕：{episode.get('subtitlePath') or '（无）'}",
            f"结果：{'成功' if result.get('success') else '失败'}",
            f"输出：{result.get('output')}",
        ]
        if result.get("command"):
            lines.append(f"命令：{result['command']}")
        if result.get("error"):
            lines.append(f"错误：{result['error']}")
        lines.append("-" * 60 + " ffmpeg 输出 " + "-" * 60)
        lines.append(result.get("log") or "（无输出）")
        log_file.write_text("\n".join(lines), encoding="utf-8", errors="replace")
    except OSError:
        # 日志写入失败不影响渲染结果
        pass


def _tail_lines(text: str, count: int) -> list[str]:
    """取日志末尾若干非空行"""
    if not text:
        return []
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-count:]

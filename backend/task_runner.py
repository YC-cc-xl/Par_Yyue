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
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import models
import system_monitor
from ffmpeg_render import (
    render_episode,
    concat_videos,
    locate_ffmpeg,
    terminate_process,
    resolve_encoder,
    probe_media,
    ENGINE_LABELS,
    FfmpegNotFound,
)
from scanner import scan_episodes, OUTPUT_DIR_NAME

# 输出目录（相对处理来源根目录）
MERGE_FILE_NAME = "合并视频.mp4"

# 任务日志中单集 ffmpeg 输出的保留行数
LOG_TAIL_LINES = 12

# 高负载任务判定阈值：负载分 = (像素数 / 1280×720) × 时长(秒)。
# 超过阈值视为「高负载任务」，用独立信号量限制其并发，避免多个大视频同时抢占全部 CPU；
# 短任务不受影响，可继续跑满线程池，从而提升整体吞吐。
HIGH_LOAD_SCORE = 900.0

# ---------------------------------------------------------------------------
# 运行时进度注册表（供 GET /api/compose/tasks/{id}/progress 读取）
#   task_id → { totalGroups, doneGroups, success, failed,
#               episodes: { 分集序号: {index,name,total,done,success,failed,status,
#                                     startTs,endTs,duration,durationText} } }
# 只在内存中维护：后端重启后任务本身就中断了，不需要持久化。
# ---------------------------------------------------------------------------
_RUN_STATE: dict[int, dict[str, Any]] = {}

# 同时保护注册表与渲染过程中的统计/日志写入，保证进度不被读到中间态
_STATE_LOCK = threading.RLock()

# 取消意向：任务尚未登记到注册表（仍在扫描阶段）时先记在这里，
# 待 _execute 注册运行时状态后立即采纳，避免「点取消但来不及生效」的竞态
_CANCEL_REQUESTED: set[int] = set()

# 取消时刻（wall clock）：取消后线程池收尾仍会写日志、更新 updated_at，
# 为了让「已运行时长」在取消瞬间定格，这里单独记下取消时刻供接口换算
_CANCEL_AT: dict[int, float] = {}

# 运行中的 ffmpeg 进程：task_id → {Popen}，供取消时「立即强杀」
_PROCS: dict[int, set[subprocess.Popen]] = {}


def get_cancel_at(task_id: int) -> float | None:
    """返回任务的取消时刻（epoch 秒），未取消返回 None"""
    with _STATE_LOCK:
        return _CANCEL_AT.get(task_id)


def register_proc(task_id: int, proc: subprocess.Popen) -> None:
    """登记刚启动的 ffmpeg 进程；若该任务已取消则立刻终止（关闭竞态窗口）"""
    with _STATE_LOCK:
        _PROCS.setdefault(task_id, set()).add(proc)
        canceled = bool((_RUN_STATE.get(task_id) or {}).get("canceled"))
    if canceled:
        terminate_process(proc)


def release_proc(task_id: int, proc: subprocess.Popen) -> None:
    """渲染结束后出表"""
    with _STATE_LOCK:
        procs = _PROCS.get(task_id)
        if procs is not None:
            procs.discard(proc)


def kill_task_procs(task_id: int) -> int:
    """立即强杀该任务当前所有在跑的 ffmpeg 进程（先取快照，出锁后再杀）"""
    with _STATE_LOCK:
        procs = list(_PROCS.get(task_id) or ())
    for proc in procs:
        terminate_process(proc)
    return len(procs)


def is_task_canceled(task_id: int) -> bool:
    """该任务是否已被请求取消"""
    with _STATE_LOCK:
        state = _RUN_STATE.get(task_id)
        return bool(state and state.get("canceled"))


def request_cancel(task_id: int) -> bool:
    """请求取消任务：置标志 + 记录取消时刻 + 立即强杀在跑的 ffmpeg

    返回 True 表示取消请求已受理；False 表示该任务已在取消中。
    """
    with _STATE_LOCK:
        state = _RUN_STATE.get(task_id)
        if state is None:
            # 仍在扫描阶段：先记意向，_execute 注册运行态后会立即采纳
            _CANCEL_REQUESTED.add(task_id)
            _CANCEL_AT[task_id] = time.time()
        elif state.get("canceled"):
            return False
        else:
            state["canceled"] = True
            _CANCEL_AT[task_id] = time.time()
    # 出锁后再强杀，避免持锁等待 taskkill
    kill_task_procs(task_id)
    return True


def _format_duration(seconds: float) -> str:
    """秒数 → 时:分:秒（如 00:02:15）"""
    total = int(round(max(0.0, float(seconds))))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def get_task_progress(task_id: int) -> dict[str, Any] | None:
    """读取任务的进度快照（任务不存在或未开始返回 None）"""
    with _STATE_LOCK:
        state = _RUN_STATE.get(task_id)
        if state is None:
            return None
        episodes = sorted(state["episodes"].values(), key=lambda ep: ep["index"])
        # 全局双时长：渲染总时长取各分集累加（前端进度统计只使用它，排队不计入）
        render_total = sum(float(ep.get("renderSeconds") or 0.0) for ep in episodes)
        queue_total = sum(float(ep.get("queueSeconds") or 0.0) for ep in episodes)
        return {
            "totalGroups": state["totalGroups"],
            "doneGroups": state["doneGroups"],
            "success": state["success"],
            "failed": state["failed"],
            # 全局：FFmpeg 实际渲染总时长 / 排队等待总时长（均附带 h:m:s 文本）
            "renderSeconds": round(render_total, 3),
            "renderText": _format_duration(render_total),
            "queueSeconds": round(queue_total, 3),
            "queueText": _format_duration(queue_total),
            # 分集条目：duration/durationText 仅表示本集 FFmpeg 实际渲染耗时，
            # queueText 为排队等待时长；分集表格既有字段（done/success/failed/status）保持不变
            "episodes": [
                {
                    "index": ep["index"],
                    "name": ep["name"],
                    "total": ep["total"],
                    "done": ep["done"],
                    "success": ep["success"],
                    "failed": ep["failed"],
                    "status": ep["status"],
                    "duration": ep.get("duration"),
                    "durationText": ep.get("durationText") or "",
                    "queueText": ep.get("queueText") or "",
                }
                for ep in episodes
            ],
        }


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
    """线程池并发数：启用并行处理时取最大任务数，否则串行（1）

    默认值 = CPU 核心数的一半（并发过高会导致 CPU/磁盘 IO 打满，反而拖慢整体速度）。
    """
    performance = config.get("performance") or {}
    if not performance.get("parallelEnabled", True):
        return 1
    default_workers = max(1, _cpu_count() // 2)
    try:
        workers = int(performance.get("maxWorkers") or default_workers)
    except (TypeError, ValueError):
        workers = default_workers
    return max(1, min(workers, 32))


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


def _estimate_load(media: dict[str, Any], output_cfg: dict[str, Any]) -> float:
    """按（分辨率 × 时长）预估单个任务的 CPU 负载分

    以 1280×720 为基准：负载分 = (像素数 / 基准像素) × 时长(秒)。
    分辨率越高、时长越长，负载分越大；用于「高负载任务限制并发」与长任务错峰排队。
    """
    width = int(media.get("width") or output_cfg.get("width") or 1280)
    height = int(media.get("height") or output_cfg.get("height") or 720)
    duration = float(media.get("duration") or 0.0)
    base_pixels = 1280 * 720
    return round(max(1, width * height) / base_pixels * max(1.0, duration), 2)


def _saturation_reason() -> str:
    """把当前资源饱和原因格式化为可读文本（供日志展示）"""
    snap = system_monitor.snapshot()
    parts: list[str] = []
    if snap.get("cpuHigh"):
        parts.append(f"CPU {snap.get('cpuPercent', 0)}%")
    if snap.get("ioHigh"):
        io = float(snap.get("diskReadMBps") or 0.0) + float(snap.get("diskWriteMBps") or 0.0)
        parts.append(f"磁盘IO {io:.1f}MB/s")
    detail = "、".join(parts) if parts else "CPU/磁盘IO"
    return f"系统资源占用过高（{detail}）"


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
        models.append_task_log(task_id, f"[错误] 任务执行异常：{exc}", "ERROR")
        models.update_task(task_id, status="failed")
    finally:
        # 收尾：清掉该任务的进程登记，避免长期运行后注册表无限增长
        with _STATE_LOCK:
            _PROCS.pop(task_id, None)
            _CANCEL_REQUESTED.discard(task_id)


def _execute(task_id: int, task: dict[str, Any]) -> None:
    config = models.load_config(task.get("config"))
    source_dir = task["source_dir"]

    # ---- 0. 前置检查：ffmpeg 必须可用 ----
    try:
        ffmpeg_path = locate_ffmpeg()
    except FfmpegNotFound:
        ffmpeg_path = None
    if ffmpeg_path is None:
        models.append_task_log(task_id, "[错误] 未找到 ffmpeg.exe，无法开始渲染", "ERROR")
        models.update_task(task_id, status="failed")
        return

    # ---- 1. 重新扫描，保证与实际文件一致 ----
    scan = scan_episodes(source_dir)
    episodes = scan["episodes"]
    if not episodes:
        models.append_task_log(task_id, "[错误] 未扫描到任何分集", "ERROR")
        models.update_task(task_id, total=0, status="failed")
        return

    output_root = Path(source_dir) / OUTPUT_DIR_NAME
    output_root.mkdir(parents=True, exist_ok=True)
    log_dir = output_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    workers = _resolve_workers(config)
    threads = _resolve_threads(config)
    output_cfg = config.get("output") or {}
    performance = config.get("performance") or {}

    # 进程防护参数：可配置超时（0/缺省=用默认）与单进程内存上限（0=不限）
    try:
        task_timeout = int(performance.get("taskTimeoutSec") or 0)
    except (TypeError, ValueError):
        task_timeout = 0
    try:
        memory_limit = int(performance.get("memoryLimitMB") or 0)
    except (TypeError, ValueError):
        memory_limit = 0

    # 系统资源监控：写入用户配置的饱和阈值并启动后台采样线程（幂等）
    system_monitor.configure(
        cpu_threshold=performance.get("cpuThreshold"),
        io_threshold_mbps=performance.get("ioThresholdMBps"),
    )
    system_monitor.start()

    # ---- 2. 组装渲染工作项：一组「合格素材（mp4+mp3+srt 同名）」= 一个渲染单位 ----
    jobs: list[dict[str, Any]] = []
    for episode in episodes:
        for group in episode.get("groups") or []:
            # 预解析缓存里已含分辨率/时长（扫描阶段预热），直接读用于负载预估与排序
            media = probe_media(Path(group["videoPath"]))
            score = _estimate_load(media, output_cfg)
            jobs.append(
                {
                    "index": episode["index"],
                    "name": episode["name"],
                    "groupName": group["name"],
                    "videoPath": group["videoPath"],
                    "audioPath": group["audioPath"],
                    "subtitlePath": group["subtitlePath"],
                    "missing": [],
                    "duration": float(media.get("duration") or 0.0),
                    "loadScore": score,
                    "highLoad": score >= HIGH_LOAD_SCORE,
                }
            )

    # 任务智能调度排序：短素材优先执行（短任务快速释放线程，提升整体吞吐）；
    # 长/高负载素材自动错峰排在后面，避免多个长视频同时抢占全部 CPU。
    jobs.sort(key=lambda job: (job["duration"], job["index"], job["groupName"]))
    high_load_jobs = sum(1 for job in jobs if job["highLoad"])

    # 运行时进度：全局 + 每个分集独立维护，供前端表格逐行显示进度与状态
    state: dict[str, Any] = {
        "totalGroups": len(jobs),
        "doneGroups": 0,
        "success": 0,
        "failed": 0,
        "canceled": False,
        "episodes": {
            episode["index"]: {
                "index": episode["index"],
                "name": episode["name"],
                "total": len(episode.get("groups") or []),
                "done": 0,
                "success": 0,
                "failed": 0,
                "status": "pending",
                # 双时长：queueSeconds=入队→ffmpeg 真正启动的排队等待；renderSeconds=ffmpeg 实际编码耗时
                "queueSeconds": 0.0,
                "renderSeconds": 0.0,
                "duration": None,
                "durationText": "",
                "queueText": "",
            }
            for episode in episodes
        },
    }
    with _STATE_LOCK:
        _RUN_STATE[task_id] = state
        # 采纳「扫描阶段就已点击取消」的意向，避免取消请求丢失
        if task_id in _CANCEL_REQUESTED:
            _CANCEL_REQUESTED.discard(task_id)
            state["canceled"] = True
    canceled = state["canceled"]

    models.update_task(task_id, total=len(episodes), success=0, failed=0, progress=0.0, status="running")
    models.append_task_log(task_id, "=" * 60)
    models.append_task_log(
        task_id,
        f"[任务] 共 {len(episodes)} 集，有效素材 {len(jobs)} 组，并发数 {workers}，"
        f"每进程编码线程 {threads}",
        "INFO",
    )
    # 编码引擎：页面选 auto 时按 NVENC > QSV > AMF 优先级自动挑选可用硬件编码器，
    # 不可用则降级 CPU 软编码；这里解析一次并写入日志（字幕硬烧仍由 CPU 完成）
    engine_key, engine_codec = resolve_encoder(output_cfg, ffmpeg_path)
    models.append_task_log(
        task_id,
        f"[任务] 分辨率 {output_cfg.get('width', 1280)}×{output_cfg.get('height', 720)}，"
        f"码率 {output_cfg.get('bitrate', 2000)}kbps，编码预设 {output_cfg.get('preset') or 'fast'}，"
        f"CRF/CQ {(output_cfg.get('crf') or 0) or '未启用'}，"
        f"编码引擎 {ENGINE_LABELS.get(str(output_cfg.get('engine') or 'auto').lower(), '自动')}"
        f" → {ENGINE_LABELS.get(engine_key, engine_key)}（{engine_codec}）"
        f"{'，字幕硬烧由 CPU 完成' if (config.get('subtitle') or {}).get('enabled') else ''}，"
        f"输出目录：{output_root}",
        "INFO",
    )
    # 调度策略与进程防护参数（本次生效口径）
    models.append_task_log(
        task_id,
        f"[调度] 短素材优先执行；高负载任务 {high_load_jobs} 组（阈值 {HIGH_LOAD_SCORE:g}）限制并发；"
        f"任务超时 {task_timeout if task_timeout > 0 else '默认 21600'} 秒，"
        f"单进程内存上限 {memory_limit if memory_limit > 0 else '不限制'}MB，"
        f"资源保护阈值 CPU {performance.get('cpuThreshold', 90)}% / 磁盘IO "
        f"{performance.get('ioThresholdMBps', 0)}MB/s（0=不启用）",
        "INFO",
    )

    # 被跳过的素材逐条提示，明确告知缺少哪个文件（不统计、不渲染）
    for episode in episodes:
        for reason in episode.get("skipped") or []:
            models.append_task_log(
                task_id,
                f"[第{episode['index']}集 {episode['name']}] 跳过 {reason}",
                "WARN",
            )
    models.append_task_log(task_id, "=" * 60)

    if not jobs:
        models.append_task_log(
            task_id,
            "[错误] 没有任何一组合格素材（视频/音频需同名配对），无法开始渲染",
            "ERROR",
        )
        models.update_task(task_id, progress=0.0, status="failed")
        return

    # ---- 3. 并发渲染各素材组 ----
    # 输出结构：自动合成输出/<分集目录名>/<素材组名>.mp4（同一分集多组互不覆盖）
    outputs: dict[int, list[tuple[str, Path]]] = {}   # 分集序号 → [(组名, 输出文件)]

    # 取消意向在渲染开始前就已生效 → 直接收敛，不提交任何渲染任务
    if state["canceled"]:
        models.append_task_log(task_id, "[取消] 任务在开始渲染前已被取消", "WARN")
        models.update_task(task_id, status="canceled")
        return

    # 高负载任务并发闸门：把大视频同时运行的数量压到并发数的一半，
    # 避免多个长任务同时抢占全部 CPU（短任务不经过闸门，可跑满线程池）
    high_load_gate = threading.Semaphore(max(1, workers // 2))

    def render_one(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Path, float, float]:
        """渲染单组素材（在线程池中执行）

        返回 (job, result, out_file, queue_seconds, render_seconds)：
          - queue_seconds：任务入队 → ffmpeg 进程真正启动之间的排队等待
          - render_seconds：ffmpeg 启动 → 渲染结束的实际编码耗时（不含排队）
        """
        if job.get("highLoad"):
            # 高负载任务先取得闸门，限制其并发数量
            with high_load_gate:
                return _render_one_inner(job)
        return _render_one_inner(job)

    def _render_one_inner(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Path, float, float]:
        episode_dir = output_root / job["name"]
        episode_dir.mkdir(parents=True, exist_ok=True)
        out_file = episode_dir / f"{job['groupName']}.mp4"

        enqueue_ts = job.get("enqueueTs") or time.monotonic()
        spawn_holder: dict[str, float] = {}

        def _on_spawn(proc: subprocess.Popen) -> None:
            # ffmpeg 真正启动的瞬间 = 渲染计时起点（与入队时刻之差即排队等待）
            spawn_holder["ts"] = time.monotonic()
            register_proc(task_id, proc)
            system_monitor.track_pid(proc.pid)   # 纳入磁盘读写汇总

        def _on_release(proc: subprocess.Popen) -> None:
            release_proc(task_id, proc)
            system_monitor.untrack_pid(proc.pid)

        result = render_episode(
            job,
            out_file,
            config,
            work_sub_name=f"sub_{task_id}_{job['index']:03d}_{job['groupName']}.ass",
            threads=threads,
            timeout=task_timeout,
            memory_limit_mb=memory_limit,
            # 交出 ffmpeg 句柄：取消时可立即强杀；is_canceled 用于区分「被强杀」与真实失败
            on_spawn=_on_spawn,
            on_release=_on_release,
            is_canceled=lambda: state["canceled"],
        )
        ended = time.monotonic()
        spawn_ts = spawn_holder.get("ts", ended)
        _write_episode_log(log_dir / f"{job['name']}__{job['groupName']}.log", job, result)
        return (
            job,
            result,
            out_file,
            max(0.0, spawn_ts - enqueue_ts),   # 排队等待时长
            max(0.0, ended - spawn_ts),        # FFmpeg 实际渲染时长
        )

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as pool:
        futures: dict[Any, dict[str, Any]] = {}
        paused_logged = False
        for job in jobs:
            if state["canceled"]:
                break
            # 系统资源监控 + 动态并发保护：CPU 持续高于阈值 / 磁盘 IO 满载时，
            # 自动临时停止提交新任务，等待资源回落再继续入队，避免系统卡死。
            if system_monitor.is_saturated():
                if not paused_logged:
                    paused_logged = True
                    models.append_task_log(
                        task_id, f"[资源] {_saturation_reason()}，暂停提交新任务，等待回落…", "WARN"
                    )
                while not state["canceled"] and system_monitor.is_saturated():
                    time.sleep(1.0)
                if paused_logged and not state["canceled"]:
                    paused_logged = False
                    models.append_task_log(task_id, "[资源] 系统资源已回落，继续提交任务", "INFO")
            job["enqueueTs"] = time.monotonic()   # 入队时刻（统计排队等待时长用）
            futures[pool.submit(render_one, job)] = job
        for future in as_completed(futures):
            job = futures[future]
            label = f"[第{job['index']}集 {job['name']} · {job['groupName']}]"
            queue_seconds = render_seconds = None
            try:
                job, result, out_file, queue_seconds, render_seconds = future.result()
            except Exception as exc:  # 单组异常不影响其它素材组
                job, result, out_file = job, {"success": False, "error": f"渲染线程异常：{exc}"}, None

            with _STATE_LOCK:
                episode_state = state["episodes"][job["index"]]

                if result.get("canceled"):
                    # 被取消强杀：不计入 failed、不推进进度，仅记录终止日志（含两套时长）
                    models.append_task_log(
                        task_id,
                        f"{label} 已终止（任务取消）｜排队 {_format_duration(queue_seconds or 0.0)}，"
                        f"已渲染 {_format_duration(render_seconds or 0.0)}",
                        "WARN",
                    )
                else:
                    episode_state["done"] += 1

                    # 双时长累加：排队等待 与 FFmpeg 实际渲染 分别统计
                    episode_state["queueSeconds"] += max(0.0, queue_seconds or 0.0)
                    episode_state["renderSeconds"] += max(0.0, render_seconds or 0.0)

                    if result.get("success"):
                        state["success"] += 1
                        episode_state["success"] += 1
                        if out_file is not None:
                            outputs.setdefault(job["index"], []).append((job["groupName"], out_file))
                        models.append_task_log(
                            task_id,
                            f"{label} 成功 → {out_file}｜排队 {_format_duration(queue_seconds or 0.0)}，"
                            f"渲染 {_format_duration(render_seconds or 0.0)}",
                            "INFO",
                        )
                        for warning in result.get("warnings") or []:
                            models.append_task_log(task_id, f"{label} 警告：{warning}", "WARN")
                    else:
                        state["failed"] += 1
                        episode_state["failed"] += 1
                        models.append_task_log(
                            task_id,
                            f"{label} 失败：{result.get('error', '未知错误')}｜排队 "
                            f"{_format_duration(queue_seconds or 0.0)}，"
                            f"渲染 {_format_duration(render_seconds or 0.0)}",
                            "ERROR",
                        )
                        for line in _tail_lines(result.get("log") or "", LOG_TAIL_LINES):
                            models.append_task_log(task_id, f"{label} | {line}", "DEBUG")

                    state["doneGroups"] += 1
                    # 分集状态：有失败 → failed；全部完成且无失败 → success；否则渲染中
                    if episode_state["failed"]:
                        episode_state["status"] = "failed"
                    elif episode_state["total"] and episode_state["done"] >= episode_state["total"]:
                        episode_state["status"] = "success"
                    else:
                        episode_state["status"] = "running"

                    # 该集所有组都已返回 → 结算本集双时长并写入日志（存入任务数据）
                    if (
                        episode_state["total"]
                        and episode_state["done"] >= episode_state["total"]
                        and not episode_state["durationText"]
                    ):
                        render_total = episode_state["renderSeconds"]
                        queue_total = episode_state["queueSeconds"]
                        # duration/durationText 仅表示 FFmpeg 实际渲染时长（排队不计入）
                        episode_state["duration"] = round(render_total, 3)
                        episode_state["durationText"] = _format_duration(render_total)
                        episode_state["queueText"] = _format_duration(queue_total)
                        models.append_task_log(
                            task_id,
                            f"[第{job['index']}集 {job['name']}] 本集渲染完成，"
                            f"FFmpeg 渲染耗时 {episode_state['durationText']}，"
                            f"排队等待 {episode_state['queueText']}",
                            "INFO",
                        )

                    models.update_task(
                        task_id,
                        success=state["success"],
                        failed=state["failed"],
                        progress=state["doneGroups"] / state["totalGroups"] * 100.0,
                    )

            # 已请求取消：当前这组（可能已被强杀）处理完后，撤销尚未开始的组并停止
            if state["canceled"]:
                for pending in futures:
                    pending.cancel()
                break

    # ---- 4. 合并视频（取消时跳过，避免继续产出半成品合并件） ----
    if state["canceled"]:
        models.append_task_log(task_id, "[取消] 任务已取消，跳过合并视频", "WARN")
    elif output_cfg.get("mergeVideo"):
        _build_merged(task_id, config, output_root, outputs, episodes)

    # ---- 5. 收敛任务状态 ----
    # 全局双时长：FFmpeg 实际渲染总时长（排队不计入）/ 排队等待总时长
    render_total = sum(float(ep.get("renderSeconds") or 0.0) for ep in state["episodes"].values())
    queue_total = sum(float(ep.get("queueSeconds") or 0.0) for ep in state["episodes"].values())
    if state["canceled"]:
        # 取消：不把进度推到 100%，进度条保持停止更新；最终状态 canceled（终态）
        models.update_task(task_id, status="canceled")
        models.append_task_log(
            task_id,
            f"[取消] 任务已取消，成功 {state['success']} 组，失败 {state['failed']} 组，"
            f"已完成 {state['doneGroups']} / {state['totalGroups']} 组素材"
            f"｜FFmpeg 渲染总耗时 {_format_duration(render_total)}，"
            f"排队等待总时长 {_format_duration(queue_total)}",
            "WARN",
        )
        return

    models.update_task(task_id, progress=100.0)
    final_status = "completed" if state["success"] > 0 else "failed"
    models.update_task(task_id, status=final_status)
    models.append_task_log(
        task_id,
        f"[完成] 成功 {state['success']} 组，失败 {state['failed']} 组，"
        f"共 {len(episodes)} 集 / {state['totalGroups']} 组素材"
        f"｜FFmpeg 渲染总耗时 {_format_duration(render_total)}，"
        f"排队等待总时长 {_format_duration(queue_total)}",
        "ERROR" if state["failed"] else "INFO",
    )


# ---------------------------------------------------------------------------
# 合并视频
# ---------------------------------------------------------------------------

def _build_merged(
    task_id: int,
    config: dict[str, Any],
    output_root: Path,
    outputs: dict[int, list[tuple[str, Path]]],
    episodes: list[dict[str, Any]],
) -> None:
    """按页面选项生成合并视频

    - 创建合并视频：把全部成功素材组按「分集序号 → 组名」顺序首尾拼接
    - 合并原视频与第一集：在合并视频最前面拼接【第一集第一组素材的原始视频】（作为片头）
    """
    output_cfg = config.get("output") or {}
    performance = config.get("performance") or {}
    try:
        task_timeout = int(performance.get("taskTimeoutSec") or 0)
    except (TypeError, ValueError):
        task_timeout = 0
    try:
        memory_limit = int(performance.get("memoryLimitMB") or 0)
    except (TypeError, ValueError):
        memory_limit = 0

    ordered: list[Path] = []
    for index in sorted(outputs):
        for _, out_file in sorted(outputs[index], key=lambda item: item[0].lower()):
            ordered.append(out_file)
    if not ordered:
        models.append_task_log(task_id, "[合并] 没有可合并的成功素材组，跳过合并", "WARN")
        return

    parts: list[Path] = []
    if output_cfg.get("mergeWithFirst"):
        first_episode = next((ep for ep in episodes if ep.get("groups")), None)
        first_video = Path(first_episode["groups"][0]["videoPath"]) if first_episode else None
        if first_video and first_video.is_file():
            parts.append(first_video)
            models.append_task_log(task_id, f"[合并] 已加入第一集原视频作为片头：{first_video.name}", "INFO")
        else:
            models.append_task_log(task_id, "[合并] 未找到第一集原视频，跳过片头", "WARN")

    parts.extend(ordered)

    merged_file = output_root / MERGE_FILE_NAME
    models.append_task_log(task_id, f"[合并] 开始拼接 {len(parts)} 个片段 → {merged_file}", "INFO")

    # 合并阶段的 ffmpeg 同样登记进程：取消时可立即强杀并清理半成品，并纳入磁盘读写汇总
    def _on_spawn(proc: subprocess.Popen) -> None:
        register_proc(task_id, proc)
        system_monitor.track_pid(proc.pid)

    def _on_release(proc: subprocess.Popen) -> None:
        release_proc(task_id, proc)
        system_monitor.untrack_pid(proc.pid)

    proc_callbacks = {
        "on_spawn": _on_spawn,
        "on_release": _on_release,
        "is_canceled": lambda: is_task_canceled(task_id),
        "timeout": task_timeout,
        "memory_limit_mb": memory_limit,
    }

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
            preset=output_cfg.get("preset"),
            crf=output_cfg.get("crf"),
            engine=output_cfg.get("engine"),
            **proc_callbacks,
        )
    else:
        result = concat_videos(parts, merged_file, **proc_callbacks)

    if result.get("canceled"):
        models.append_task_log(task_id, "[合并] 已终止（任务取消），半成品已清理", "WARN")
    elif result.get("success"):
        models.append_task_log(task_id, f"[合并] 完成 → {merged_file}", "INFO")
    else:
        models.append_task_log(task_id, f"[合并] 失败：{result.get('error', '未知错误')}", "ERROR")
        for line in _tail_lines(result.get("log") or "", LOG_TAIL_LINES):
            models.append_task_log(task_id, f"[合并] | {line}", "DEBUG")


# ---------------------------------------------------------------------------
# 日志落盘
# ---------------------------------------------------------------------------

def _write_episode_log(log_file: Path, episode: dict[str, Any], result: dict[str, Any]) -> None:
    """把单集的 ffmpeg 完整日志写入文件（满足「捕获全部 stdout/stderr」）"""
    try:
        lines = [
            f"分集：第{episode['index']}集 {episode['name']}",
            f"素材组：{episode.get('groupName') or episode['name']}",
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

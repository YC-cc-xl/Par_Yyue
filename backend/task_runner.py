"""批量合成任务调度（多项目 + 双调度模式）

职责：
  1. 接收任务 ID，在后台线程中编排整批渲染流程（接口立即返回，前端轮询进度）
  2. 按【调度模式】决定并发方式：
       - multi  高性能多任务：线程池并发；硬件编码（nvenc/qsv/amf）任务串行排队
       - serial 低配单任务串行：同一时刻只跑 1 个 ffmpeg，固定项目优先
  3. 按【调度策略】决定任务提交顺序：项目优先（跑完一个项目再下一个）/ 均分（跨项目轮转）
  4. 逐组调用 ffmpeg_render.render_episode，实时更新成功/失败/进度/日志（带项目标签）
  5. 断点续跑：扫描阶段判定已存在成品的组直接跳过，不重复渲染
  6. 合并视频：按【项目】独立控制，某个项目的分集全部结束后才合并；全局合并互斥（串行）
  7. 项目级暂停/取消：只影响该项目，不影响其他项目

关于两个性能参数（页面语义）：
  - 最大任务数 / 启用并行处理：控制同时渲染的素材组数量（线程池 worker 数）
  - 性能上限(%)：控制【单个 ffmpeg 进程】可用的编码线程数 = CPU核数 × 上限比例
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import models
import system_monitor
from ffmpeg_render import (
    render_episode,
    concat_videos,
    require_ffmpeg,
    terminate_process,
    resolve_encoder,
    probe_media,
    ENGINE_LABELS,
    FfmpegNotFound,
)
from scanner import scan_projects, DONE_DIR_NAME

# 合并成片文件名（落在【一级项目根目录】下）
MERGE_FILE_NAME = "合并视频.mp4"

# 单个分集内 ffmpeg 完整日志的存放子目录（位于该分集的【处理完成】目录内）
LOG_DIR_NAME = "日志"

# 任务日志中单组 ffmpeg 输出的保留行数
LOG_TAIL_LINES = 12

# 高负载任务判定阈值：负载分 = (像素数 / 1280×720) × 时长(秒)。
# 超过阈值视为「高负载任务」，用独立信号量限制其并发，避免多个大视频同时抢占全部 CPU；
# 短任务不受影响，可继续跑满线程池，从而提升整体吞吐。
HIGH_LOAD_SCORE = 900.0

# ---------------------------------------------------------------------------
# 运行时进度注册表（供 GET /api/compose/tasks/{id}/progress 读取）
#   task_id → { totalGroups, doneGroups, success, failed, canceled,
#               projects: { 项目序号: { index,name,root,paused,canceled,
#                                        total,done,success,failed,resumed,
#                                        mergeVideo,mergeWithFirst,mergeChecked,merged } },
#               episodes: { "项目-分集" 复合键: { 同前 + projectIndex/projectName } } }
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

# 运行中的 ffmpeg 进程：task_id → { 项目序号: {Popen} }，供取消时「立即强杀」；
# 按项目分桶是为了支持「单独取消某一个项目」而不误杀其他项目的进程
_PROCS: dict[int, dict[int, set[subprocess.Popen]]] = {}

# 全局合并互斥锁：同一时间只允许 1 个合并任务在跑，避免磁盘 IO 抢占
_MERGE_GATE = threading.Lock()


def get_cancel_at(task_id: int) -> float | None:
    """返回任务的取消时刻（epoch 秒），未取消返回 None"""
    with _STATE_LOCK:
        return _CANCEL_AT.get(task_id)


def register_proc(task_id: int, proc: subprocess.Popen, project_index: int = 0) -> None:
    """登记刚启动的 ffmpeg 进程；若任务或该项目已取消则立刻终止（关闭竞态窗口）"""
    with _STATE_LOCK:
        procs = _PROCS.setdefault(task_id, {}).setdefault(project_index, set())
        procs.add(proc)
        state = _RUN_STATE.get(task_id) or {}
        canceled = bool(state.get("canceled")) or bool(
            ((state.get("projects") or {}).get(project_index) or {}).get("canceled")
        )
    if canceled:
        terminate_process(proc)


def release_proc(task_id: int, proc: subprocess.Popen) -> None:
    """渲染结束后出表（进程可能登记在任意项目桶里，逐个桶移除）"""
    with _STATE_LOCK:
        buckets = _PROCS.get(task_id)
        if buckets:
            for procs in buckets.values():
                procs.discard(proc)


def kill_task_procs(task_id: int) -> int:
    """立即强杀该任务当前所有在跑的 ffmpeg 进程（先取快照，出锁后再杀）"""
    with _STATE_LOCK:
        snapshot = [proc for procs in (_PROCS.get(task_id) or {}).values() for proc in procs]
    for proc in snapshot:
        terminate_process(proc)
    return len(snapshot)


def kill_project_procs(task_id: int, project_index: int) -> int:
    """立即强杀【指定项目】在跑的 ffmpeg 进程（不影响其他项目的进程）"""
    with _STATE_LOCK:
        snapshot = list((_PROCS.get(task_id) or {}).get(project_index) or ())
    for proc in snapshot:
        terminate_process(proc)
    return len(snapshot)


def is_task_canceled(task_id: int) -> bool:
    """该任务是否已被请求取消"""
    with _STATE_LOCK:
        state = _RUN_STATE.get(task_id)
        return bool(state and state.get("canceled"))


def is_project_canceled(task_id: int, project_index: int) -> bool:
    """指定项目是否已被取消（供合并阶段的取消判定使用）"""
    with _STATE_LOCK:
        state = _RUN_STATE.get(task_id)
        project = ((state or {}).get("projects") or {}).get(project_index)
        return bool(project and project.get("canceled"))


def request_cancel(task_id: int) -> bool:
    """请求取消整个任务：置标志 + 记录取消时刻 + 立即强杀在跑的 ffmpeg

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


def set_project_paused(task_id: int, project_index: int, paused: bool) -> bool:
    """暂停/恢复指定项目（暂停只拦住未开始的任务，不打断在跑的 ffmpeg）

    返回 False 表示任务未在运行或该项目不存在。
    """
    with _STATE_LOCK:
        state = _RUN_STATE.get(task_id)
        project = ((state or {}).get("projects") or {}).get(project_index)
        if project is None or (state or {}).get("canceled"):
            return False
        project["paused"] = bool(paused)
    return True


def cancel_project(task_id: int, project_index: int) -> bool:
    """取消指定项目：置标志 + 强杀该项目在跑的 ffmpeg（其他项目不受影响）

    该项目尚未开始的素材组会被跳过（计入进度），已完成的成品保留。
    """
    with _STATE_LOCK:
        state = _RUN_STATE.get(task_id)
        project = ((state or {}).get("projects") or {}).get(project_index)
        if project is None or project.get("canceled"):
            return False
        project["canceled"] = True
        project["paused"] = False
    # 出锁后再强杀，避免持锁等待 taskkill
    kill_project_procs(task_id, project_index)
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
        episodes = sorted(
            state["episodes"].values(), key=lambda ep: (ep["projectIndex"], ep["index"])
        )
        # 全局双时长：渲染总时长取各分集累加（前端进度统计只使用它，排队不计入）
        render_total = sum(float(ep.get("renderSeconds") or 0.0) for ep in episodes)
        queue_total = sum(float(ep.get("queueSeconds") or 0.0) for ep in episodes)
        projects = sorted(state["projects"].values(), key=lambda item: item["index"])
        return {
            "totalGroups": state["totalGroups"],
            "doneGroups": state["doneGroups"],
            "success": state["success"],
            "failed": state["failed"],
            # 断点续跑：扫描阶段判定成品已存在而跳过的组数
            "resumed": state.get("resumed", 0),
            # 项目被取消后跳过（未渲染）的组数
            "skippedGroups": state.get("skippedGroups", 0),
            # 全局：FFmpeg 实际渲染总时长 / 排队等待总时长（均附带 h:m:s 文本）
            "renderSeconds": round(render_total, 3),
            "renderText": _format_duration(render_total),
            "queueSeconds": round(queue_total, 3),
            "queueText": _format_duration(queue_total),
            # 分集条目：duration/durationText 仅表示本集 FFmpeg 实际渲染耗时，
            # queueText 为排队等待时长；key 为「项目序号-分集序号」复合键（避免跨项目撞号）
            "episodes": [
                {
                    "key": ep["key"],
                    "projectIndex": ep["projectIndex"],
                    "projectName": ep["projectName"],
                    "index": ep["index"],
                    "name": ep["name"],
                    "total": ep["total"],
                    "done": ep["done"],
                    "success": ep["success"],
                    "failed": ep["failed"],
                    "skipped": ep.get("skipped", 0),
                    "status": ep["status"],
                    "duration": ep.get("duration"),
                    "durationText": ep.get("durationText") or "",
                    "queueText": ep.get("queueText") or "",
                }
                for ep in episodes
            ],
            # 项目级汇总：供页面按项目展示进度与「暂停/恢复/取消」按钮状态
            "projects": [
                {
                    "index": item["index"],
                    "name": item["name"],
                    "root": item["root"],
                    "paused": item["paused"],
                    "canceled": item["canceled"],
                    "mergeVideo": item["mergeVideo"],
                    "mergeWithFirst": item["mergeWithFirst"],
                    "merged": item["merged"],
                    "total": item["total"],
                    "done": item["done"],
                    "success": item["success"],
                    "failed": item["failed"],
                    "resumed": item["resumed"],
                }
                for item in projects
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
# 并发参数解析 / 调度参数解析
# ---------------------------------------------------------------------------

def _cpu_count() -> int:
    return os.cpu_count() or 4


def _resolve_schedule(config: dict[str, Any]) -> tuple[str, str]:
    """解析调度模式与调度策略

    模式 scheduleMode：
      multi  高性能多任务模式（适配 20 核 + 独显，多任务并发，硬件编码串行）
      serial 低配单任务串行模式（同一时刻只跑 1 个 ffmpeg，固定项目优先）
    策略 schedulePolicy：
      project_first 项目优先（跑完一个项目再进下一个）
      even          均分任务（跨项目轮转，齐头并进）
    串行模式下策略固定为 project_first（页面下拉会置灰）。
    """
    performance = config.get("performance") or {}
    mode = str(performance.get("scheduleMode") or "multi").strip().lower()
    if mode not in ("multi", "serial"):
        mode = "multi"
    policy = str(performance.get("schedulePolicy") or "project_first").strip().lower()
    if policy not in ("project_first", "even"):
        policy = "project_first"
    if mode == "serial":
        policy = "project_first"
    return mode, policy


def _resolve_workers(config: dict[str, Any]) -> int:
    """线程池并发数：串行模式固定 1；多任务模式取最大任务数，关闭并行则为 1

    默认值 = CPU 核心数的一半（并发过高会导致 CPU/磁盘 IO 打满，反而拖慢整体速度）。
    """
    mode, _ = _resolve_schedule(config)
    if mode == "serial":
        return 1
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


def _effective_output_cfg(config: dict[str, Any]) -> dict[str, Any]:
    """计算本次任务实际生效的输出配置

    低配串行模式：固定 libx264 + preset=veryfast + CRF=24（页面只做提示，后端强制），
    保证普通 6 核 16G 机器也能稳定跑出「约 60 分钟 1 条 9 小时 1080p 全字幕成片」。
    """
    output_cfg = dict(config.get("output") or {})
    mode, _ = _resolve_schedule(config)
    if mode == "serial":
        output_cfg["engine"] = "cpu"
        output_cfg["preset"] = "veryfast"
        output_cfg["crf"] = 24
    return output_cfg


def _resolve_projects(config: dict[str, Any]) -> list[dict[str, Any]]:
    """解析请求体里的项目列表（每个项目 = 一个一级根目录 + 该项自己的合并勾选）

    优先读 projects（新协议：每项自带 mergeVideo/mergeWithFirst，实现「按项目独立合并」）；
    缺失时回退到旧协议 sourceDirs / sourceDir，并用 output.mergeVideo 作为全局开关。
    """
    result: list[dict[str, Any]] = []
    for item in config.get("projects") or []:
        if isinstance(item, str):
            if item.strip():
                result.append({"root": item.strip(), "mergeVideo": False, "mergeWithFirst": False})
        elif isinstance(item, dict):
            root = str(item.get("root") or "").strip()
            if root:
                result.append(
                    {
                        "root": root,
                        "mergeVideo": bool(item.get("mergeVideo")),
                        "mergeWithFirst": bool(item.get("mergeWithFirst")),
                    }
                )
    if result:
        return result

    # 旧协议兼容：sourceDirs 列表 / sourceDir 单值，合并开关取 output 配置
    output_cfg = config.get("output") or {}
    dirs = [str(d).strip() for d in (config.get("sourceDirs") or []) if str(d or "").strip()]
    if not dirs and str(config.get("sourceDir") or "").strip():
        dirs = [str(config["sourceDir"]).strip()]
    return [
        {
            "root": root,
            "mergeVideo": bool(output_cfg.get("mergeVideo")),
            "mergeWithFirst": bool(output_cfg.get("mergeWithFirst")),
        }
        for root in dirs
    ]


def _order_jobs(jobs: list[dict[str, Any]], policy: str) -> list[dict[str, Any]]:
    """按调度策略决定任务提交顺序

    项目优先：项目 → 分集 → 短素材优先（跑完一个项目再进下一个，优先出片）
    均分任务：跨项目轮转（round-robin），每个项目内部仍按分集/短素材优先，多个项目齐头并进
    """
    if policy != "even":
        return sorted(
            jobs,
            key=lambda job: (
                job["projectIndex"],
                job["episodeIndex"],
                job["duration"],
                job["groupName"],
            ),
        )

    buckets: dict[int, list[dict[str, Any]]] = {}
    for job in jobs:
        buckets.setdefault(job["projectIndex"], []).append(job)
    for bucket in buckets.values():
        bucket.sort(key=lambda job: (job["episodeIndex"], job["duration"], job["groupName"]))

    ordered: list[dict[str, Any]] = []
    keys = sorted(buckets)
    cursor = {key: 0 for key in keys}
    while True:
        progressed = False
        for key in keys:
            if cursor[key] < len(buckets[key]):
                ordered.append(buckets[key][cursor[key]])
                cursor[key] += 1
                progressed = True
        if not progressed:
            break
    return ordered


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


def _job_label(job: dict[str, Any]) -> str:
    """任务日志前缀：带上项目标识与分集归属，便于多项目并行时区分来源

    直接使用【项目文件夹名】作为项目标识（避免与「项目」二字重复叠加）。
    """
    return (
        f"[{job['projectName']}｜第{job['episodeIndex']}集 "
        f"{job['episodeName']} · {job['groupName']}]"
    )


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

    # ---- 0. 前置检查：ffmpeg 必须可用 ----
    try:
        ffmpeg_path = require_ffmpeg()
    except FfmpegNotFound:
        ffmpeg_path = None
    if ffmpeg_path is None:
        models.append_task_log(task_id, "[错误] 未找到 ffmpeg.exe，无法开始渲染", "ERROR")
        models.update_task(task_id, status="failed")
        return

    # ---- 1. 解析项目列表并重新扫描，保证与实际文件一致 ----
    projects_cfg = _resolve_projects(config)
    if not projects_cfg:
        models.append_task_log(task_id, "[错误] 未指定任何一级项目根目录", "ERROR")
        models.update_task(task_id, total=0, status="failed")
        return

    try:
        scan = scan_projects([item["root"] for item in projects_cfg])
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        models.append_task_log(task_id, f"[错误] 扫描项目目录失败：{exc}", "ERROR")
        models.update_task(task_id, total=0, status="failed")
        return

    scanned_projects = scan["projects"]
    # 把每个项目的合并勾选（页面项目列表内）写回扫描结果
    for position, project in enumerate(scanned_projects):
        cfg_project = projects_cfg[position] if position < len(projects_cfg) else {}
        project["mergeVideo"] = bool(cfg_project.get("mergeVideo"))
        project["mergeWithFirst"] = bool(cfg_project.get("mergeWithFirst"))

    episodes = [episode for project in scanned_projects for episode in project["episodes"]]
    if not episodes:
        models.append_task_log(task_id, "[错误] 未扫描到任何分集", "ERROR")
        models.update_task(task_id, total=0, status="failed")
        return

    # ---- 2. 调度与进程防护参数解析 ----
    mode, policy = _resolve_schedule(config)
    workers = _resolve_workers(config)
    threads = _resolve_threads(config)
    output_cfg = _effective_output_cfg(config)
    performance = config.get("performance") or {}
    # 渲染/合并统一使用「生效后的输出配置」（串行模式下已被强制为 libx264 veryfast CRF24）
    effective_config = {**config, "output": output_cfg}

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

    # ---- 3. 组装渲染工作项：一组「合格素材（mp4+mp3+srt 同名）」= 一个渲染单位 ----
    # 断点续跑：成品已存在的组直接跳过（计入成功），不参与渲染
    jobs: list[dict[str, Any]] = []
    resumed_total = 0
    for project in scanned_projects:
        for episode in project["episodes"]:
            for group in episode["groups"]:
                if group.get("resumed"):
                    resumed_total += 1
                    continue
                media = probe_media(Path(group["videoPath"]))
                score = _estimate_load(media, output_cfg)
                jobs.append(
                    {
                        "projectIndex": project["index"],
                        "projectName": project["name"],
                        "projectRoot": project["root"],
                        "episodeIndex": episode["index"],
                        "episodeName": episode["name"],
                        "episodeDir": episode["dirPath"],
                        "key": f"{project['index']}-{episode['index']}",
                        # render_episode 直接读取以下三个字段定位源素材
                        "name": episode["name"],
                        "groupName": group["name"],
                        "videoPath": group["videoPath"],
                        "audioPath": group["audioPath"],
                        "subtitlePath": group["subtitlePath"],
                        # 成品输出路径：分集目录 / 处理完成 / 组名.mp4
                        "outputPath": group["outputPath"],
                        "duration": float(media.get("duration") or 0.0),
                        "loadScore": score,
                        "highLoad": score >= HIGH_LOAD_SCORE,
                    }
                )

    jobs = _order_jobs(jobs, policy)
    high_load_jobs = sum(1 for job in jobs if job["highLoad"])

    # 运行时进度：全局 + 每个项目 + 每个分集，供前端表格逐行显示进度与状态
    state: dict[str, Any] = {
        "totalGroups": sum(
            len(episode["groups"]) for project in scanned_projects for episode in project["episodes"]
        ),
        "doneGroups": 0,
        "success": 0,
        "failed": 0,
        "resumed": resumed_total,
        "skippedGroups": 0,
        "canceled": False,
        "projects": {},
        "episodes": {},
    }
    for project in scanned_projects:
        project_resumed = sum(episode["resumedCount"] for episode in project["episodes"])
        state["projects"][project["index"]] = {
            "index": project["index"],
            "name": project["name"],
            "root": project["root"],
            "paused": False,
            "canceled": False,
            "mergeVideo": project["mergeVideo"],
            "mergeWithFirst": project["mergeWithFirst"],
            # mergeChecked：该项目是否已做过合并判定（避免重复触发）
            "mergeChecked": False,
            "merged": False,
            "total": project["taskTotal"],
            "done": 0,
            "success": 0,
            "failed": 0,
            "resumed": project_resumed,
        }
        # 续跑组视为已完成：全局/项目/分集三个层级的计数都直接置初值
        state["projects"][project["index"]]["done"] = project_resumed
        state["projects"][project["index"]]["success"] = project_resumed
        state["doneGroups"] += project_resumed
        state["success"] += project_resumed

        for episode in project["episodes"]:
            key = f"{project['index']}-{episode['index']}"
            resumed_here = episode["resumedCount"]
            total_here = len(episode["groups"])
            state["episodes"][key] = {
                "key": key,
                "projectIndex": project["index"],
                "projectName": project["name"],
                "index": episode["index"],
                "name": episode["name"],
                "total": total_here,
                "done": resumed_here,
                "success": resumed_here,
                "failed": 0,
                "skipped": 0,
                "status": "success" if total_here and resumed_here >= total_here else "pending",
                # 双时长：queueSeconds=入队→ffmpeg 真正启动的排队等待；renderSeconds=ffmpeg 实际编码耗时
                "queueSeconds": 0.0,
                "renderSeconds": 0.0,
                "duration": None,
                "durationText": "",
                "queueText": "",
            }

    with _STATE_LOCK:
        _RUN_STATE[task_id] = state
        # 采纳「扫描阶段就已点击取消」的意向，避免取消请求丢失
        if task_id in _CANCEL_REQUESTED:
            _CANCEL_REQUESTED.discard(task_id)
            state["canceled"] = True

    models.update_task(
        task_id, total=len(episodes), success=state["success"], failed=0, progress=0.0, status="running"
    )
    models.append_task_log(task_id, "=" * 60)
    models.append_task_log(
        task_id,
        f"[任务] 共 {len(scanned_projects)} 个项目 / {len(episodes)} 集，"
        f"合格素材 {state['totalGroups']} 组（其中 {resumed_total} 组成品已存在，跳过渲染），"
        f"调度模式 {'低配串行' if mode == 'serial' else '高性能多任务'}，"
        f"调度策略 {'项目优先' if policy == 'project_first' else '均分任务'}，"
        f"并发数 {workers}，每进程编码线程 {threads}",
        "INFO",
    )

    # 编码引擎：页面选 auto 时按 NVENC > QSV > AMF 优先级自动挑选可用硬件编码器，
    # 不可用则降级 CPU 软编码；这里解析一次并写入日志（字幕硬烧仍由 CPU 完成）
    engine_key, engine_codec = resolve_encoder(output_cfg, ffmpeg_path)
    hardware_encode = engine_key in ("nvenc", "qsv", "amf")
    models.append_task_log(
        task_id,
        f"[任务] 分辨率 {output_cfg.get('width', 1280)}×{output_cfg.get('height', 720)}，"
        f"码率 {output_cfg.get('bitrate', 2000)}kbps，编码预设 {output_cfg.get('preset') or 'fast'}，"
        f"CRF/CQ {(output_cfg.get('crf') or 0) or '未启用'}，"
        f"编码引擎 {ENGINE_LABELS.get(str(output_cfg.get('engine') or 'auto').lower(), '自动')}"
        f" → {ENGINE_LABELS.get(engine_key, engine_key)}（{engine_codec}）"
        f"{'，字幕硬烧由 CPU 完成' if (config.get('subtitle') or {}).get('enabled') else ''}，"
        f"输出目录：各分集下的【{DONE_DIR_NAME}】",
        "INFO",
    )
    # 调度闸门说明：串行模式 / 硬件编码串行 / 纯 CPU 并发，三种语义分别提示
    if mode == "serial":
        gate_desc = "低配单任务串行模式：同一时刻仅 1 个 ffmpeg 渲染任务，任务排队串行执行（固定项目优先）"
    elif hardware_encode:
        gate_desc = "硬件编码任务串行排队（同一时刻仅 1 个硬件编码进程），CPU 软编码任务可并发"
    else:
        gate_desc = "未检测到可用硬件编码器，全部任务以 CPU 软编码并发提交"
    models.append_task_log(
        task_id,
        f"[调度] {gate_desc}；高负载任务 {high_load_jobs} 组（阈值 {HIGH_LOAD_SCORE:g}）限制并发；"
        f"任务超时 {task_timeout if task_timeout > 0 else '默认 21600'} 秒，"
        f"单进程内存上限 {memory_limit if memory_limit > 0 else '不限制'}MB，"
        f"资源保护阈值 CPU {performance.get('cpuThreshold', 90)}% / 磁盘IO "
        f"{performance.get('ioThresholdMBps', 0)}MB/s（0=不启用）",
        "INFO",
    )

    # 项目级告警（如某个二级分集缺【视频】/【音频】文件夹）
    for project in scanned_projects:
        for warning in project.get("warnings") or []:
            models.append_task_log(task_id, f"[{project['name']}] {warning}", "WARN")
    # 被跳过的素材逐条提示，明确告知缺少哪个文件（不统计、不渲染）
    for project in scanned_projects:
        for episode in project["episodes"]:
            for reason in episode.get("skipped") or []:
                models.append_task_log(
                    task_id,
                    f"[{project['name']} 第{episode['index']}集 {episode['name']}] 跳过 {reason}",
                    "WARN",
                )
    models.append_task_log(task_id, "=" * 60)

    if not jobs and resumed_total == 0:
        models.append_task_log(
            task_id,
            "[错误] 没有任何一组合格素材（视频/音频需同名配对），无法开始渲染",
            "ERROR",
        )
        models.update_task(task_id, progress=0.0, status="failed")
        return

    # ---- 4. 并发/串行渲染各素材组 ----
    # 输出结构：各分集目录下的【处理完成】文件夹；按项目 + 分集汇总已成功成品，供项目级合并使用
    project_outputs: dict[int, dict[int, list[tuple[str, Path]]]] = {}

    # 全局取消意向在渲染开始前就已生效 → 直接收敛，不提交任何渲染任务
    if state["canceled"]:
        models.append_task_log(task_id, "[取消] 任务在开始渲染前已被取消", "WARN")
        models.update_task(task_id, status="canceled")
        return

    # 高负载任务并发闸门：把大视频同时运行的数量压到并发数的一半，
    # 避免多个长任务同时抢占全部 CPU（短任务不经过闸门，可跑满线程池）
    high_load_gate = threading.Semaphore(max(1, workers // 2))
    # 硬件编码闸门：同一时刻只允许 1 个硬件编码 ffmpeg（nvenc/qsv/amf），CPU 软编码不受限。
    # 注意：单个 ffmpeg 进程内部是「CPU 滤镜+字幕 → 编码器」串行的，无法拆分，
    # 因此这里对整个进程加闸门；未取得闸门的组会在工作线程内等待，不占用额外 CPU。
    hardware_gate = threading.Semaphore(1) if (hardware_encode and mode == "multi") else None

    def render_one(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Path, float, float]:
        """渲染单组素材（在线程池中执行）：按需先取得硬件编码闸门与高负载闸门

        返回 (job, result, out_file, queue_seconds, render_seconds)：
          - queue_seconds：任务入队 → ffmpeg 进程真正启动之间的排队等待
          - render_seconds：ffmpeg 启动 → 渲染结束的实际编码耗时（不含排队）
        """
        acquired: list[threading.Semaphore] = []
        try:
            if hardware_gate is not None:
                hardware_gate.acquire()
                acquired.append(hardware_gate)
            if job.get("highLoad"):
                high_load_gate.acquire()
                acquired.append(high_load_gate)
            return _render_one_inner(job)
        finally:
            # 逆序释放，与获取顺序相反
            for gate in reversed(acquired):
                gate.release()

    def _render_one_inner(
        job: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], Path, float, float]:
        # 成品固定落在本分集的【处理完成】目录内，渲染前确保目录存在
        out_file = Path(job["outputPath"])
        out_file.parent.mkdir(parents=True, exist_ok=True)
        # 每个分集内保留一份完整 ffmpeg 日志，便于失败时排查
        log_file = out_file.parent / LOG_DIR_NAME / f"{job['episodeName']}__{job['groupName']}.log"

        enqueue_ts = job.get("enqueueTs") or time.monotonic()
        spawn_holder: dict[str, float] = {}

        def _on_spawn(proc: subprocess.Popen) -> None:
            # ffmpeg 真正启动的瞬间 = 渲染计时起点（与入队时刻之差即排队等待）
            spawn_holder["ts"] = time.monotonic()
            register_proc(task_id, proc, job["projectIndex"])
            system_monitor.track_pid(proc.pid)   # 纳入磁盘读写汇总

        def _on_release(proc: subprocess.Popen) -> None:
            release_proc(task_id, proc)
            system_monitor.untrack_pid(proc.pid)

        result = render_episode(
            job,
            out_file,
            effective_config,
            work_sub_name=(
                f"sub_{task_id}_{job['projectIndex']:02d}_{job['episodeIndex']:03d}_"
                f"{job['groupName']}.ass"
            ),
            threads=threads,
            timeout=task_timeout,
            memory_limit_mb=memory_limit,
            # 交出 ffmpeg 句柄：取消时可立即强杀；is_canceled 用于区分「被强杀」与真实失败
            on_spawn=_on_spawn,
            on_release=_on_release,
            is_canceled=lambda: state["canceled"] or bool(
                (state["projects"].get(job["projectIndex"]) or {}).get("canceled")
            ),
        )
        ended = time.monotonic()
        spawn_ts = spawn_holder.get("ts", ended)
        _write_episode_log(log_file, job, result)
        return (
            job,
            result,
            out_file,
            max(0.0, spawn_ts - enqueue_ts),   # 排队等待时长
            max(0.0, ended - spawn_ts),        # FFmpeg 实际渲染时长
        )

    def _push_progress() -> None:
        """把最新成功/失败数写回任务记录（含总进度百分比）"""
        models.update_task(
            task_id,
            success=state["success"],
            failed=state["failed"],
            progress=(
                state["doneGroups"] / state["totalGroups"] * 100.0
                if state["totalGroups"]
                else 0.0
            ),
        )

    def _skip_pending(job: dict[str, Any], reason: str) -> None:
        """项目被取消后，跳过尚未开始的素材组：计入进度并标记该集为已取消"""
        with _STATE_LOCK:
            episode_state = state["episodes"][job["key"]]
            episode_state["done"] += 1
            episode_state["skipped"] += 1
            episode_state["status"] = "canceled"
            project_state = state["projects"][job["projectIndex"]]
            project_state["done"] += 1
            state["skippedGroups"] += 1
            state["doneGroups"] += 1
            models.append_task_log(
                task_id, f"{_job_label(job)} 跳过（{reason}）", "WARN"
            )
            _push_progress()

    def _consume(future: Future) -> None:
        """回收一个已完成的渲染任务：更新统计、日志与项目级成品清单"""
        job = futures.pop(future)
        label = _job_label(job)
        queue_seconds = render_seconds = None
        try:
            job, result, out_file, queue_seconds, render_seconds = future.result()
        except Exception as exc:  # 单组异常不影响其它素材组
            result, out_file = {"success": False, "error": f"渲染线程异常：{exc}"}, None

        with _STATE_LOCK:
            episode_state = state["episodes"][job["key"]]
            project_state = state["projects"][job["projectIndex"]]

            if result.get("canceled"):
                # 被强杀：不计入 failed、不推进进度，仅记录终止日志（含两套时长）
                models.append_task_log(
                    task_id,
                    f"{label} 已终止（项目/任务取消）｜排队 {_format_duration(queue_seconds or 0.0)}，"
                    f"已渲染 {_format_duration(render_seconds or 0.0)}",
                    "WARN",
                )
                # 该组被强杀：若本集没有已成功的组，状态记为「已取消」，
                # 避免分集表格长期停留在「渲染中 / 待处理」（尚未开始即被取消时原状态为 pending）
                if episode_state["status"] in ("running", "pending"):
                    episode_state["status"] = "canceled"
            else:
                episode_state["done"] += 1
                project_state["done"] += 1

                # 双时长累加：排队等待 与 FFmpeg 实际渲染 分别统计
                episode_state["queueSeconds"] += max(0.0, queue_seconds or 0.0)
                episode_state["renderSeconds"] += max(0.0, render_seconds or 0.0)

                if result.get("success"):
                    state["success"] += 1
                    episode_state["success"] += 1
                    project_state["success"] += 1
                    if out_file is not None:
                        project_outputs.setdefault(job["projectIndex"], {}).setdefault(
                            job["episodeIndex"], []
                        ).append((job["groupName"], out_file))
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
                    project_state["failed"] += 1
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
                        f"[{job['projectName']} 第{job['episodeIndex']}集 {job['episodeName']}] "
                        f"本集渲染完成，FFmpeg 渲染耗时 {episode_state['durationText']}，"
                        f"排队等待 {episode_state['queueText']}",
                        "INFO",
                    )

                _push_progress()

    def _merge_finished_projects() -> None:
        """检查是否有项目已跑完全部分集 → 触发（或跳过）该项目自己的合并

        合并按【项目】独立控制：项目 A 勾选只合并 A，项目 B 不勾选就不合并 B；
        全部合并经由 _MERGE_GATE 串行执行，避免磁盘 IO 抢占。
        软件重启后不会自动触发合并：合并只发生在一次任务执行过程中。
        """
        for project_index, project_state in sorted(state["projects"].items()):
            if project_state["mergeChecked"] or project_state["canceled"]:
                continue
            if project_state["total"] > 0 and project_state["done"] < project_state["total"]:
                continue   # 该项目还有未完成的素材组
            if project_state["total"] == 0 and project_state["resumed"] == 0:
                project_state["mergeChecked"] = True
                continue   # 该项目没有任何素材组，无需合并
            project_state["mergeChecked"] = True
            if state["canceled"]:
                continue
            if not project_state["mergeVideo"]:
                # 未勾选合并：仅记录日志，不做任何拼接
                models.append_task_log(
                    task_id,
                    f"[{project_state['name']}] 未勾选「创建合并视频」，跳过合并",
                    "INFO",
                )
                continue
            scanned_project = next(
                (item for item in scanned_projects if item["index"] == project_index), None
            )
            if scanned_project is None:
                continue
            outputs = project_outputs.get(project_index, {})
            with _MERGE_GATE:   # 全局互斥：同一时间只跑 1 个合并任务
                merged_ok = _build_merged(
                    task_id, effective_config, scanned_project, outputs, project_state
                )
            project_state["merged"] = bool(merged_ok)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as pool:
        futures: dict[Future, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = list(jobs)
        paused_logged = False
        paused_projects: set[int] = set()

        # 交错循环：提交可运行任务 → 回收已完成任务 → 再提交，直到队列与线程池都清空。
        # 交错而非「一次性全提交」的原因：项目级暂停/取消需要能立即拦住未开始的任务。
        while (pending or futures) and not state["canceled"]:
            # ---- 资源饱和保护：CPU 持续高于阈值 / 磁盘 IO 满载时暂停提交新任务 ----
            saturated = system_monitor.is_saturated()
            if saturated and pending and not paused_logged:
                paused_logged = True
                models.append_task_log(
                    task_id, f"[资源] {_saturation_reason()}，暂停提交新任务，等待回落…", "WARN"
                )
            elif not saturated and paused_logged:
                paused_logged = False
                models.append_task_log(task_id, "[资源] 系统资源已回落，继续提交任务", "INFO")

            # ---- 提交阶段：跳过暂停/已取消项目的任务，其余按「在跑任务数 < 并发数」逐步入队 ----
            progressed = False
            if not saturated:
                for job in list(pending):
                    if state["canceled"] or system_monitor.is_saturated():
                        break
                    # 只提交到「已提交任务数 = 并发数」为止，多余任务留在 pending 队列里：
                    # 这样项目级暂停/取消才能及时拦住「尚未开始」的素材组
                    # （串行模式并发数=1 → 同一时刻池中仅 1 个 ffmpeg 渲染任务）
                    if len(futures) >= workers:
                        break
                    project_state = state["projects"][job["projectIndex"]]
                    if project_state["canceled"]:
                        # 项目已取消：跳过该组（计入进度），不提交渲染
                        pending.remove(job)
                        _skip_pending(job, "项目已取消")
                        progressed = True
                        continue
                    if project_state["paused"]:
                        # 项目暂停：本轮不提交，留到恢复后再入队（已提交的任务继续跑完）
                        if job["projectIndex"] not in paused_projects:
                            paused_projects.add(job["projectIndex"])
                            models.append_task_log(
                                task_id,
                                f"[项目{project_state['name']}] 已暂停，未开始的任务暂不提交；"
                                f"已开始的渲染将正常跑完",
                                "WARN",
                            )
                        continue
                    if job["projectIndex"] in paused_projects:
                        paused_projects.discard(job["projectIndex"])
                        models.append_task_log(
                            task_id, f"[项目{project_state['name']}] 已恢复，继续提交剩余任务", "INFO"
                        )
                    job["enqueueTs"] = time.monotonic()   # 入队时刻（统计排队等待时长用）
                    futures[pool.submit(render_one, job)] = job
                    pending.remove(job)
                    progressed = True

            # ---- 回收阶段：等待至少一个任务完成，处理结果 ----
            if futures:
                done, _ = wait(list(futures), timeout=1.0, return_when=FIRST_COMPLETED)
                for future in done:
                    _consume(future)
            elif not progressed:
                # 剩余任务全部处于暂停状态：稍作等待后再检查一次
                time.sleep(0.5)

            # 已请求取消：当前批次处理完后，撤销尚未开始的组并停止
            if state["canceled"]:
                for future in futures:
                    future.cancel()
                break

            # ---- 项目级合并：某个项目分集全部结束即触发（勾选了才合并） ----
            try:
                _merge_finished_projects()
            except Exception as exc:   # 合并异常不影响其他项目渲染
                models.append_task_log(task_id, f"[合并] 合并阶段异常：{exc}", "ERROR")

        # 取消后仍有在跑任务时，线程池退出前会把它们收尾（已被强杀，很快结束）
        for future in list(futures):
            if future.cancelled():
                futures.pop(future, None)

    # ---- 5. 收尾：把尚未判定合并的项目补一次判定（例如最后一批任务完成后立即取消/暂停） ----
    if not state["canceled"]:
        try:
            _merge_finished_projects()
        except Exception as exc:
            models.append_task_log(task_id, f"[合并] 合并阶段异常：{exc}", "ERROR")

    # ---- 6. 收敛任务状态 ----
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
        f"[完成] 成功 {state['success']} 组（含 {state['resumed']} 组成品已存在跳过渲染），"
        f"失败 {state['failed']} 组，共 {len(scanned_projects)} 个项目 / {len(episodes)} 集 / "
        f"{state['totalGroups']} 组素材｜FFmpeg 渲染总耗时 {_format_duration(render_total)}，"
        f"排队等待总时长 {_format_duration(queue_total)}",
        "ERROR" if state["failed"] else "INFO",
    )


# ---------------------------------------------------------------------------
# 合并视频（按项目独立）
# ---------------------------------------------------------------------------

def _build_merged(
    task_id: int,
    config: dict[str, Any],
    project: dict[str, Any],
    outputs: dict[int, list[tuple[str, Path]]],
    project_state: dict[str, Any],
) -> bool:
    """为一个项目生成合并视频

    - 创建合并视频：把该项目全部成功素材组按「分集序号 → 组名」顺序首尾拼接
    - 合并原视频与第一集：在合并视频最前面拼接【该项目第一集第一组素材的原始视频】作为片头
    - 成片输出到【一级项目根目录】下的 合并视频.mp4
    返回是否合并成功（未勾选/无可合并素材时返回 False）。
    """
    project_name = project_state["name"]
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
    for episode_index in sorted(outputs):
        for _, out_file in sorted(outputs[episode_index], key=lambda item: item[0].lower()):
            ordered.append(out_file)
    if not ordered:
        models.append_task_log(
            task_id, f"[{project_name} 合并] 没有可合并的成功素材组，跳过合并", "WARN"
        )
        return False

    parts: list[Path] = []
    if project_state["mergeWithFirst"]:
        first_episode = next(
            (episode for episode in project.get("episodes") or [] if episode.get("groups")), None
        )
        first_video = Path(first_episode["groups"][0]["videoPath"]) if first_episode else None
        if first_video and first_video.is_file():
            parts.append(first_video)
            models.append_task_log(
                task_id,
                f"[{project_name} 合并] 已加入第一集原视频作为片头：{first_video.name}",
                "INFO",
            )
        else:
            models.append_task_log(
                task_id, f"[{project_name} 合并] 未找到第一集原视频，跳过片头", "WARN"
            )

    parts.extend(ordered)

    # 合并成片输出到一级项目根目录
    merged_file = Path(project["root"]) / MERGE_FILE_NAME
    models.append_task_log(
        task_id,
        f"[{project_name} 合并] 开始拼接 {len(parts)} 个片段 → {merged_file}",
        "INFO",
    )

    # 合并阶段的 ffmpeg 同样登记进程：取消时可立即强杀并清理半成品，并纳入磁盘读写汇总
    def _on_spawn(proc: subprocess.Popen) -> None:
        register_proc(task_id, proc, project_state["index"])
        system_monitor.track_pid(proc.pid)

    def _on_release(proc: subprocess.Popen) -> None:
        release_proc(task_id, proc)
        system_monitor.untrack_pid(proc.pid)

    proc_callbacks = {
        "on_spawn": _on_spawn,
        "on_release": _on_release,
        # 任务级取消或本项目取消，都会终止合并（避免继续产出半成品）
        "is_canceled": lambda: is_task_canceled(task_id)
        or is_project_canceled(task_id, project_state["index"]),
        "timeout": task_timeout,
        "memory_limit_mb": memory_limit,
    }

    if project_state["mergeWithFirst"] and len(parts) > 1:
        # 片头是原始视频，编码参数与合成结果不同，直接 copy 拼接会失败，故重新编码；
        # 同时按页面配置的分辨率/码率统一重编码到 1080p，避免成片被片头的原始分辨率带偏
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
        models.append_task_log(
            task_id, f"[{project_name} 合并] 已终止（项目/任务取消），半成品已清理", "WARN"
        )
        return False
    if result.get("success"):
        models.append_task_log(task_id, f"[{project_name} 合并] 完成 → {merged_file}", "INFO")
        return True

    models.append_task_log(
        task_id,
        f"[{project_name} 合并] 失败：{result.get('error', '未知错误')}",
        "ERROR",
    )
    for line in _tail_lines(result.get("log") or "", LOG_TAIL_LINES):
        models.append_task_log(task_id, f"[{project_name} 合并] | {line}", "DEBUG")
    return False


# ---------------------------------------------------------------------------
# 日志落盘
# ---------------------------------------------------------------------------

def _write_episode_log(log_file: Path, job: dict[str, Any], result: dict[str, Any]) -> None:
    """把单组的 ffmpeg 完整日志写入文件（满足「捕获全部 stdout/stderr」）"""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"项目：{job.get('projectName')}",
            f"分集：第{job.get('episodeIndex')}集 {job.get('episodeName')}",
            f"素材组：{job.get('groupName')}",
            f"视频：{job.get('videoPath')}",
            f"配音：{job.get('audioPath')}",
            f"字幕：{job.get('subtitlePath') or '（无）'}",
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

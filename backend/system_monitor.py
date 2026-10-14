"""系统资源监控（零依赖：仅用 ctypes 调用 Win32 API，不引入第三方库）

职责：
  1. CPU 使用率：GetSystemTimes 取系统内核/用户/空闲时间差计算（全系统口径）
  2. 磁盘读写：GetProcessIoCounters 汇总「本进程 + 正在渲染的 ffmpeg 子进程」的读写字节，
     换算为 MB/s —— 这正是本应用实际产生的磁盘压力
  3. 饱和判定：CPU 连续多次高于阈值，或磁盘吞吐持续高于阈值 → 判定饱和，
     调度层据此「临时停止提交新任务」，资源回落再继续入队

降级策略：非 Windows 或 API 不可用时全部返回「不支持」，绝不影响渲染。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections import deque
from typing import Any, Optional

# 默认阈值：CPU 持续高于 90% 视为饱和；磁盘阈值默认 0 = 不启用（无设备吞吐基准，交由用户配置）
DEFAULT_CPU_THRESHOLD = 90.0
DEFAULT_IO_THRESHOLD_MBPS = 0.0

# 采样间隔与「持续」判定窗口（连续 N 次超阈值才判饱和，避免瞬时尖峰误停）
SAMPLE_INTERVAL = 2.0
SUSTAIN_SAMPLES = 3

_IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# Win32 API 绑定
# ---------------------------------------------------------------------------

class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if _IS_WINDOWS else None


def _resolve_io_counters():
    """定位 GetProcessIoCounters：不同 Windows 版本导出位置不同，逐个候选尝试

    候选顺序：kernel32!K32GetProcessIoCounters → kernel32!GetProcessIoCounters
             → psapi!GetProcessIoCounters → psapi!K32GetProcessIoCounters
    全部缺失时返回 None（磁盘吞吐降级为 0，CPU 监控不受影响）。
    """
    for name in ("K32GetProcessIoCounters", "GetProcessIoCounters"):
        fn = getattr(_kernel32, name, None)
        if fn is not None:
            return fn
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
    except OSError:
        return None
    for name in ("GetProcessIoCounters", "K32GetProcessIoCounters"):
        fn = getattr(psapi, name, None)
        if fn is not None:
            return fn
    return None


if _kernel32 is not None:
    _kernel32.GetSystemTimes.argtypes = [
        ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME)
    ]
    _kernel32.GetSystemTimes.restype = ctypes.c_bool
    _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _io_counters_fn = _resolve_io_counters()
    if _io_counters_fn is not None:
        _io_counters_fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IO_COUNTERS)]
        _io_counters_fn.restype = ctypes.c_bool

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _filetime_to_int(value: _FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def _read_system_times() -> Optional[tuple[int, int, int]]:
    """返回 (空闲, 内核, 用户) 累计 100ns 计数；不可用返回 None"""
    if _kernel32 is None:
        return None
    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return _filetime_to_int(idle), _filetime_to_int(kernel), _filetime_to_int(user)


def _read_process_io(pid: int) -> Optional[tuple[int, int]]:
    """返回该进程 (累计读字节, 累计写字节)；不可用返回 None"""
    if _kernel32 is None or _io_counters_fn is None:
        return None
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        counters = _IO_COUNTERS()
        if not _io_counters_fn(handle, ctypes.byref(counters)):
            return None
        return counters.ReadTransferCount, counters.WriteTransferCount
    finally:
        _kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# 监控状态
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

_tracked_pids: set[int] = set()          # 正在渲染的 ffmpeg 子进程
_summary: dict[str, Any] = {
    "supported": _IS_WINDOWS and _kernel32 is not None and _io_counters_fn is not None,
    "cpuPercent": 0.0,
    "diskReadMBps": 0.0,
    "diskWriteMBps": 0.0,
    "processCount": 0,
    "cpuThreshold": DEFAULT_CPU_THRESHOLD,
    "ioThresholdMBps": DEFAULT_IO_THRESHOLD_MBPS,
    "cpuHigh": False,
    "ioHigh": False,
    "throttled": False,
}

_prev_cpu: Optional[tuple[int, int, int]] = None
_prev_io: dict[int, tuple[int, int]] = {}
_prev_ts: Optional[float] = None
_cpu_streak: deque[bool] = deque(maxlen=SUSTAIN_SAMPLES)
_io_streak: deque[bool] = deque(maxlen=SUSTAIN_SAMPLES)


def track_pid(pid: int) -> None:
    """登记一个正在渲染的 ffmpeg 进程（用于磁盘读写汇总）"""
    with _lock:
        _tracked_pids.add(pid)


def untrack_pid(pid: int) -> None:
    """渲染结束后注销该进程"""
    with _lock:
        _tracked_pids.discard(pid)
        _prev_io.pop(pid, None)


def configure(cpu_threshold: float | None = None,
              io_threshold_mbps: float | None = None) -> None:
    """写入用户配置的饱和阈值（CPU 百分比 / 磁盘吞吐 MB/s，0 表示不启用该判据）"""
    with _lock:
        if cpu_threshold is not None:
            _summary["cpuThreshold"] = max(0.0, min(float(cpu_threshold), 100.0))
        if io_threshold_mbps is not None:
            _summary["ioThresholdMBps"] = max(0.0, float(io_threshold_mbps))


def snapshot() -> dict[str, Any]:
    """读取最近一次采样结果（供接口/日志使用）"""
    with _lock:
        return dict(_summary)


def is_saturated() -> bool:
    """当前是否处于资源饱和状态（调度层据此暂停提交新任务）"""
    with _lock:
        return bool(_summary["throttled"])


# ---------------------------------------------------------------------------
# 采样线程
# ---------------------------------------------------------------------------

def _sample_once() -> None:
    """采样一次：CPU 占比 + 磁盘吞吐 + 饱和判定"""
    global _prev_cpu, _prev_ts
    now = time.monotonic()
    cpu_times = _read_system_times()

    with _lock:
        cpu_threshold = float(_summary["cpuThreshold"])
        io_threshold = float(_summary["ioThresholdMBps"])
        pids = list(_tracked_pids)
        prev_cpu = _prev_cpu
        prev_ts = _prev_ts

    cpu_percent = 0.0
    elapsed = (now - prev_ts) if prev_ts is not None else 0.0
    if cpu_times is not None and prev_cpu is not None and elapsed > 0:
        idle_delta = cpu_times[0] - prev_cpu[0]
        total_delta = (cpu_times[1] - prev_cpu[1]) + (cpu_times[2] - prev_cpu[2])
        if total_delta > 0:
            cpu_percent = max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))

    # 磁盘吞吐：本进程 + 全部已登记的 ffmpeg 子进程的读写增量
    read_bytes, write_bytes = _delta_io(os.getpid())
    for pid in pids:
        r, w = _delta_io(pid)
        read_bytes += r
        write_bytes += w

    mb = 1_000_000.0
    disk_read = read_bytes / mb / elapsed if elapsed > 0 else 0.0
    disk_write = write_bytes / mb / elapsed if elapsed > 0 else 0.0

    with _lock:
        _prev_cpu = cpu_times
        _prev_ts = now
        _summary["cpuPercent"] = round(cpu_percent, 1)
        _summary["diskReadMBps"] = round(disk_read, 2)
        _summary["diskWriteMBps"] = round(disk_write, 2)
        _summary["processCount"] = len(pids)

        cpu_high = cpu_threshold > 0 and cpu_percent >= cpu_threshold
        io_high = io_threshold > 0 and (disk_read + disk_write) >= io_threshold
        _cpu_streak.append(cpu_high)
        _io_streak.append(io_high)
        # 连续 SUSTAIN_SAMPLES 次超阈值才算饱和（瞬时尖峰不触发暂停）
        saturated_cpu = len(_cpu_streak) >= SUSTAIN_SAMPLES and all(_cpu_streak)
        saturated_io = len(_io_streak) >= SUSTAIN_SAMPLES and all(_io_streak)
        _summary["cpuHigh"] = saturated_cpu
        _summary["ioHigh"] = saturated_io
        _summary["throttled"] = bool(saturated_cpu or saturated_io)


def _delta_io(pid: int) -> tuple[int, int]:
    """计算某进程自上次采样以来的读写增量（读取失败返回 0）"""
    current = _read_process_io(pid)
    if current is None:
        return 0, 0
    with _lock:
        previous = _prev_io.get(pid)
        _prev_io[pid] = current
    if previous is None:
        return 0, 0
    return max(0, current[0] - previous[0]), max(0, current[1] - previous[1])


def _loop() -> None:
    while not _stop.wait(SAMPLE_INTERVAL):
        try:
            _sample_once()
        except Exception:  # 采样异常绝不能影响渲染
            continue


def start() -> None:
    """启动后台采样线程（幂等）"""
    global _thread, _prev_cpu, _prev_ts
    if not _summary["supported"]:
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
    _prev_cpu = _read_system_times()
    _prev_ts = time.monotonic()
    _thread = threading.Thread(target=_loop, name="system-monitor", daemon=True)
    _thread.start()


def stop() -> None:
    """停止采样线程（进程退出/测试用）"""
    _stop.set()

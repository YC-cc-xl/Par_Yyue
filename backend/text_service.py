"""文本处理服务层（日志 + 后台任务 + 备份恢复）

职责（全部本地离线，不联网、不调用外部 API）：
  1. 文本操作日志：复用 models.DB_PATH 数据文件，新建 TextOpLog 表，
     分级与项目现有日志体系一致（DEBUG < INFO < WARN < ERROR）；
  2. 后台任务：长文本处理放到守护线程执行，内存注册表记录进度，前端轮询查询；
  3. 备份 / 恢复：编辑器文本落盘到 data/text_backup 目录，文件名带时间戳，支持多份。

说明：models.py 属于自动合成模块既有代码，本模块「只读复用」其连接与常量，不做任何修改。
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import models

# ---------------------------------------------------------------------------
# 备份目录：与数据库同级的 data/text_backup（打包后位于用户数据目录下）
# ---------------------------------------------------------------------------
BACKUP_DIR: Path = Path(models.DB_PATH).parent / "text_backup"

# ---------------------------------------------------------------------------
# 日志表（惰性建表：首次写日志/查日志时创建，避免改动 main.py 的 lifespan）
# ---------------------------------------------------------------------------
_TABLE_LOCK = threading.Lock()
_tables_ready = False


def _ensure_tables() -> None:
    """幂等建表：文本操作日志表 TextOpLog"""
    global _tables_ready
    if _tables_ready:
        return
    with _TABLE_LOCK:
        if _tables_ready:
            return
        with models._WRITE_LOCK, models._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS TextOpLog (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id      TEXT    NOT NULL DEFAULT '',   -- 关联的后台任务 ID（同步操作留空）
                    level       TEXT    NOT NULL DEFAULT 'INFO', -- DEBUG/INFO/WARN/ERROR
                    message     TEXT    NOT NULL,
                    created_at  TEXT    NOT NULL
                )
                """
            )
        _tables_ready = True


def append_text_log(message: str, level: str = "INFO", job_id: str = "") -> None:
    """写入一条文本处理日志（分级，写库失败不影响业务主流程）"""
    try:
        _ensure_tables()
        tag = str(level).upper()
        if tag not in models.LOG_LEVELS:
            tag = "INFO"
        with models._WRITE_LOCK, models._connect() as conn:
            conn.execute(
                "INSERT INTO TextOpLog (job_id, level, message, created_at) VALUES (?, ?, ?, ?)",
                (str(job_id or ""), tag, str(message).rstrip("\n"), models._now()),
            )
    except Exception:
        # 日志属于旁路功能：任何异常都不得让文本处理失败
        pass


def list_text_logs(job_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """按时间倒序读取日志（可按 jobId 过滤，limit 收敛到 1~1000）"""
    _ensure_tables()
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    sql = "SELECT id, job_id, level, message, created_at FROM TextOpLog"
    params: list[Any] = []
    if job_id:
        sql += " WHERE job_id = ?"
        params.append(str(job_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with models._connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


# ---------------------------------------------------------------------------
# 后台任务注册表（内存维护：后端重启后任务本身已中断，无需持久化）
#   { job_id: { id, kind, status, progress, message, result, error, created_at, updated_at } }
# ---------------------------------------------------------------------------
JOB_RUNNING = "running"
JOB_SUCCESS = "success"
JOB_FAILED = "failed"

_JOBS: dict[str, dict[str, Any]] = {}
_JOB_LOCK = threading.RLock()
_MAX_JOBS = 50          # 内存中最多保留的任务条数（超出丢弃最早的已结束任务）
_KEEP_TERMINAL = 20     # 已结束任务保留条数


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _update_job(job_id: str, **fields: Any) -> None:
    """更新任务状态（加锁，避免前端轮询读到中间态）"""
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        job["updated_at"] = _now_str()


def _prune_jobs() -> None:
    """裁剪任务注册表：先清理最老的已结束任务，再兜底按创建顺序删除"""
    with _JOB_LOCK:
        if len(_JOBS) <= _MAX_JOBS:
            return
        finished = [j for j in _JOBS.values() if j["status"] != JOB_RUNNING]
        finished.sort(key=lambda j: j.get("created_at") or "")
        for job in finished[: max(0, len(finished) - _KEEP_TERMINAL)]:
            _JOBS.pop(job["id"], None)
        while len(_JOBS) > _MAX_JOBS:
            oldest = min(_JOBS.values(), key=lambda j: j.get("created_at") or "")
            if oldest["status"] == JOB_RUNNING:  # 运行中的任务不裁剪
                break
            _JOBS.pop(oldest["id"], None)


def _run_job(job_id: str, kind: str, worker: Callable[[], dict[str, Any]]) -> None:
    """后台守护线程入口：执行 worker 并回填结果/失败信息"""
    try:
        _update_job(job_id, progress=30.0, message="正在处理文本…")
        result = worker()
        _finish_job(job_id, kind, result)
    except Exception as exc:  # noqa: BLE001 - 后台线程异常必须收敛，不能让接口 500
        _update_job(job_id, status=JOB_FAILED, progress=100.0, message="处理失败", error=str(exc))
        append_text_log(f"[{kind}] 后台任务失败：{exc}", "ERROR", job_id)


def _finish_job(job_id: str, kind: str, result: dict[str, Any]) -> None:
    """任务成功收尾：写入结果、进度 100、并记一条成功日志"""
    _update_job(job_id, status=JOB_SUCCESS, progress=100.0, message="处理完成", result=result)
    length = len(result.get("text") or "")
    append_text_log(f"[{kind}] 后台任务完成，结果 {length} 字", "INFO", job_id)


def submit_job(kind: str, worker: Callable[[], dict[str, Any]]) -> str:
    """把长文本处理提交到后台守护线程，立即返回 jobId（前端据此轮询）

    worker 为无参可调用对象，需返回形如 {"text": ..., "stats": {...}} 的结果字典。
    """
    job_id = uuid.uuid4().hex
    with _JOB_LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": JOB_RUNNING,
            "progress": 10.0,
            "message": "已提交后台任务…",
            "result": None,
            "error": "",
            "created_at": _now_str(),
            "updated_at": _now_str(),
        }
    _prune_jobs()
    append_text_log(f"[{kind}] 已提交后台任务（{job_id[:8]}）", "INFO", job_id)
    threading.Thread(
        target=_run_job,
        args=(job_id, kind, worker),
        name=f"text-job-{job_id[:8]}",
        daemon=True,
    ).start()
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    """查询任务：返回副本，避免调用方直接持有内部字典"""
    with _JOB_LOCK:
        job = _JOBS.get(str(job_id or ""))
        return dict(job) if job else None


# ---------------------------------------------------------------------------
# 备份 / 恢复
# ---------------------------------------------------------------------------

def _safe_backup_path(name: str) -> Path:
    """把备份文件名解析为安全路径（白名单校验，杜绝目录穿越）"""
    cleaned = str(name or "").strip()
    if (
        not cleaned
        or cleaned != Path(cleaned).name          # 含路径分隔符 / .. 时不等
        or "/" in cleaned
        or "\\" in cleaned
        or not cleaned.lower().endswith(".txt")
    ):
        raise ValueError("备份文件名非法")
    return BACKUP_DIR / cleaned


def _file_time(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return ""


def backup_text(text: str, note: str = "") -> dict[str, Any]:
    """把当前编辑器文本备份为本地文件（带时间戳，支持多份）

    写入采用「临时文件 + os.replace」原子替换，避免异常退出留下半截文件。
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"文本备份_{stamp}.txt"
    seq = 1
    while target.exists():           # 同一秒内多次备份：追加序号，避免互相覆盖
        target = BACKUP_DIR / f"文本备份_{stamp}_{seq}.txt"
        seq += 1

    tmp = target.with_name(target.stem + ".tmp")
    tmp.write_text(text or "", encoding="utf-8")
    os.replace(tmp, target)

    size = target.stat().st_size
    suffix = f"· {note}" if note else ""
    append_text_log(f"备份文本 → {target.name}（{size} 字节）{suffix}", "INFO")
    return {"file": target.name, "size": size, "time": _file_time(target)}


def list_backups() -> list[dict[str, Any]]:
    """按修改时间倒序列出全部备份"""
    if not BACKUP_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in BACKUP_DIR.glob("*.txt"):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append({"file": path.name, "size": size, "time": _file_time(path)})
    rows.sort(key=lambda item: item.get("time") or "", reverse=True)
    return rows


def read_backup(name: str) -> dict[str, Any]:
    """读取指定备份内容（用于恢复到编辑器）；文件不存在抛 FileNotFoundError"""
    path = _safe_backup_path(name)
    if not path.exists():
        raise FileNotFoundError(f"备份文件不存在：{name}")
    content = path.read_text(encoding="utf-8", errors="replace")
    return {
        "file": path.name,
        "content": content,
        "size": path.stat().st_size,
        "time": _file_time(path),
    }

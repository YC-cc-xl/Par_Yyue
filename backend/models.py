"""SQLite3 数据层（纯标准库实现，无 ORM、无第三方依赖）

数据库仅包含两张表：
  - ComposePreset  参数预设
  - ComposeTask    批量合成任务记录

所有路径统一使用 pathlib 处理；WAL 模式保证「批量并发渲染」时读写互不阻塞。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

# ---------------------------------------------------------------------------
# 数据库位置
#  1) 优先读取环境变量 COMPOSE_DB_PATH（阶段7 由 Electron 主进程注入用户数据目录）
#  2) 默认放在 backend/data/compose.db
# ---------------------------------------------------------------------------
_DEFAULT_DB_FILE = Path(__file__).resolve().parent / "data" / "compose.db"
DB_PATH: Path = Path(os.environ.get("COMPOSE_DB_PATH") or _DEFAULT_DB_FILE)

# 写操作加锁，避免多线程（concurrent.futures）同时写库导致的竞争
_WRITE_LOCK = threading.RLock()


def _now() -> str:
    """统一的本地时间字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _open_conn() -> sqlite3.Connection:
    """创建一个数据库连接（每处调用独立连接，避免跨线程复用）"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL：读写可并发，适合多线程渲染写日志的场景
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """连接上下文：正常结束提交事务并关闭连接，异常时回滚

    注意：sqlite3.Connection 自带的 with 只管理事务、不会关闭连接，
    长时间运行（前端 1s 轮询）会持续泄漏句柄，因此这里显式关闭。
    """
    conn = _open_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """建表（幂等），应用启动时调用一次"""
    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ComposePreset (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                config      TEXT    NOT NULL,              -- 整页表单配置的 JSON 字符串
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ComposeTask (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_dir  TEXT    NOT NULL,              -- 处理来源根目录
                total       INTEGER NOT NULL DEFAULT 0,    -- 总分集数
                success     INTEGER NOT NULL DEFAULT 0,    -- 成功数量
                failed      INTEGER NOT NULL DEFAULT 0,    -- 失败数量
                progress    REAL    NOT NULL DEFAULT 0,    -- 进度百分比 0~100
                status      TEXT    NOT NULL DEFAULT 'pending', -- pending/running/completed/failed
                config      TEXT    NOT NULL,              -- 本次任务参数快照(JSON)
                log         TEXT    NOT NULL DEFAULT '',   -- 完整日志
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            )
            """
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# ---------------------------------------------------------------------------
# ComposePreset 参数预设
# ---------------------------------------------------------------------------

def list_presets() -> list[dict[str, Any]]:
    """按创建时间倒序列出全部预设"""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM ComposePreset ORDER BY id DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_preset(preset_id: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ComposePreset WHERE id = ?", (preset_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def create_preset(name: str, config: Any) -> Optional[dict[str, Any]]:
    """新增预设；config 可以是 dict 或已序列化好的 JSON 字符串"""
    now = _now()
    with _WRITE_LOCK, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO ComposePreset (name, config, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (name, _dump_config(config), now, now),
        )
        preset_id = cur.lastrowid
    return get_preset(preset_id)


def update_preset(preset_id: int, name: str, config: Any) -> Optional[dict[str, Any]]:
    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            """
            UPDATE ComposePreset
               SET name = ?, config = ?, updated_at = ?
             WHERE id = ?
            """,
            (name, _dump_config(config), _now(), preset_id),
        )
    return get_preset(preset_id)


def delete_preset(preset_id: int) -> bool:
    """删除预设，返回是否真的删除了记录"""
    with _WRITE_LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM ComposePreset WHERE id = ?", (preset_id,))
        rowcount = cur.rowcount
    return rowcount > 0


# ---------------------------------------------------------------------------
# ComposeTask 批量合成任务
# ---------------------------------------------------------------------------

# update_task 允许更新的字段白名单，防止键名注入
_TASK_UPDATABLE_FIELDS = {"total", "success", "failed", "progress", "status", "config"}


def create_task(
    source_dir: str, total: int, config: Any, status: str = "pending"
) -> Optional[dict[str, Any]]:
    """创建批量合成任务记录"""
    now = _now()
    with _WRITE_LOCK, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO ComposeTask
                (source_dir, total, success, failed, progress, status, config, log, created_at, updated_at)
            VALUES (?, ?, 0, 0, 0, ?, ?, '', ?, ?)
            """,
            (source_dir, int(total), status, _dump_config(config), now, now),
        )
        task_id = cur.lastrowid
    return get_task(task_id)


def get_task(task_id: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM ComposeTask WHERE id = ?", (task_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ComposeTask ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_task(task_id: int, **fields: Any) -> Optional[dict[str, Any]]:
    """按字段白名单更新任务"""
    update_fields = {k: v for k, v in fields.items() if k in _TASK_UPDATABLE_FIELDS}
    if not update_fields:
        return get_task(task_id)

    if "config" in update_fields:
        update_fields["config"] = _dump_config(update_fields["config"])

    # 进展类字段统一收敛到合法范围，避免前端进度条异常
    if "progress" in update_fields:
        update_fields["progress"] = max(0.0, min(100.0, float(update_fields["progress"])))
    if "total" in update_fields:
        update_fields["total"] = max(0, int(update_fields["total"]))
    if "success" in update_fields:
        update_fields["success"] = max(0, int(update_fields["success"]))
    if "failed" in update_fields:
        update_fields["failed"] = max(0, int(update_fields["failed"]))

    assignments = ", ".join(f"{key} = ?" for key in update_fields)
    params = list(update_fields.values()) + [_now(), task_id]

    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            f"UPDATE ComposeTask SET {assignments}, updated_at = ? WHERE id = ?",
            params,
        )
    return get_task(task_id)


def append_task_log(task_id: int, text: str) -> None:
    """向任务日志追加一行（并发渲染时由各集线程调用，内部已加锁）"""
    line = text.rstrip("\n") + "\n"
    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            """
            UPDATE ComposeTask
               SET log = log || ?, updated_at = ?
             WHERE id = ?
            """,
            (line, _now(), task_id),
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _dump_config(config: Any) -> str:
    """把配置统一转换为 JSON 字符串存储"""
    if config is None:
        return "{}"
    if isinstance(config, str):
        return config
    return json.dumps(config, ensure_ascii=False)


def load_config(config: Any) -> dict[str, Any]:
    """读取任务/预设时把 config 反序列化为 dict"""
    if isinstance(config, dict):
        return config
    if not config:
        return {}
    try:
        return json.loads(config)
    except (TypeError, ValueError):
        return {}

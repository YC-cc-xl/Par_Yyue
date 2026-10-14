"""FastAPI 后端入口

接口清单（与前端 src/api/index.js 一一对应）：
  GET    /api/health                    健康检查（阶段7 Electron 探测后端是否就绪）
  POST   /api/compose/scan              扫描根目录，返回分集清单
  GET    /api/compose/presets           预设列表
  POST   /api/compose/presets           新增预设
  DELETE /api/compose/presets/{id}      删除预设
  POST   /api/compose/tasks             创建批量合成任务（真正的并发渲染在 task_runner.py，阶段6）
  GET    /api/compose/tasks/{id}        查询任务进度与日志（前端 1s 轮询）

启动方式：python main.py --port 8123   （阶段7 由 Electron 主进程拉起）
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

# 允许以 `python main.py` 直接运行时，按脚本所在目录解析同级模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import models
from scanner import scan_episodes

# 阶段6 提供的并发渲染入口；未就绪时给出明确提示而不是崩溃
try:
    from task_runner import start_task_async, get_task_progress, request_cancel, get_cancel_at
except ImportError:  # pragma: no cover - 阶段6 之前会走到这里
    start_task_async = None
    get_task_progress = None
    request_cancel = None
    get_cancel_at = None

DEFAULT_PORT = 8123


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时建表（幂等）"""
    models.init_db()
    yield


app = FastAPI(title="自动合成后端", version="1.0.0", lifespan=lifespan)

# Electron 生产环境通过 file:// 加载页面，其 Origin 为 null；
# 因此放开跨域限制（服务仅监听 127.0.0.1，不对外网暴露）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    sourceDir: str = Field(..., description="分集根目录")


class PresetRequest(BaseModel):
    name: str = Field(..., description="预设名称")
    config: dict = Field(default_factory=dict, description="整页表单配置")


class BgmConfig(BaseModel):
    enabled: bool = False
    mode: str = "single_loop"
    file: str = ""
    volume: int = 20


class SubtitleConfig(BaseModel):
    enabled: bool = False
    style: dict = Field(default_factory=dict)


class OutputConfig(BaseModel):
    mergeVideo: bool = True
    mergeWithFirst: bool = False
    width: int = 1280
    height: int = 720
    bitrate: int = 2000


class PerformanceConfig(BaseModel):
    parallelEnabled: bool = True
    maxWorkers: int = 4
    perfLimit: int = 80


class TaskRequest(BaseModel):
    sourceDir: str
    bgm: BgmConfig = Field(default_factory=BgmConfig)
    subtitle: SubtitleConfig = Field(default_factory=SubtitleConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)


# ---------------------------------------------------------------------------
# 任务运行时长
#   created_at 在点击【开始处理】创建任务时写入 → 任务起始时间戳
#   updated_at 在任务进入 completed/failed 后不再变动 → 任务结束时间戳
#   （复用任务记录已有字段，不新增数据库字段）
# ---------------------------------------------------------------------------

def _parse_ts(value):
    """把 'YYYY-MM-DD HH:MM:SS' 解析为 datetime，失败返回 None"""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _format_duration(seconds):
    """秒数 → 时:分:秒（如 00:02:15）"""
    total = int(round(max(0.0, float(seconds))))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _with_runtime(task):
    """给任务记录补充「已运行时长」：运行中按当前时间计，终态按结束时间戳计

    canceled（手动取消）在取消瞬间定格：取消后线程池收尾仍会刷新 updated_at，
    因此改用渲染模块记录的「取消时刻」作为结束时间，保证计时真正停止。
    """
    started = _parse_ts(task.get("created_at"))
    status = task.get("status")
    finished = status in ("completed", "failed", "canceled")
    ended = _parse_ts(task.get("updated_at")) if finished else None
    if status == "canceled" and get_cancel_at is not None:
        cancel_at = get_cancel_at(task.get("id"))
        if cancel_at:
            ended = datetime.fromtimestamp(cancel_at)
    seconds = ((ended or datetime.now()) - started).total_seconds() if started else 0.0
    return {
        **task,
        "elapsedSeconds": int(round(seconds)),
        "elapsed": _format_duration(seconds),
        "finishedAt": task.get("updated_at", "") if finished else "",
    }


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    """健康检查：Electron 启动后端后轮询此接口判断是否就绪"""
    return {"status": "ok", "db": str(models.DB_PATH)}


@app.post("/api/compose/scan")
def scan(req: ScanRequest) -> dict:
    """扫描根目录：一级子文件夹 = 一个分集"""
    # 空字符串/纯空白必须显式拦截：Path("") 等价于 Path(".")，
    # 会被放进 scan_episodes 扫描后端进程的当前工作目录
    if not (req.sourceDir or "").strip():
        raise HTTPException(status_code=400, detail="处理来源目录不能为空")
    try:
        result = scan_episodes(req.sourceDir)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=f"没有访问权限：{e}")

    # 在原有返回值上追加两部分数据（episodes 的既有字段保持可用）：
    #   detail  → 分集明细，供页面表格渲染（一行 = 一个分集文件夹）
    #   summary → 全局汇总，供页面【全局统计栏】渲染
    detail = []
    for episode in result.get("episodes", []):
        groups = episode.get("groups") or []
        detail.append(
            {
                "index": episode["index"],
                "name": episode["name"],
                "audioCount": len(groups),                  # 合格素材组数（mp4+mp3+srt 同名）
                "videoCount": episode.get("videoCount", 0),  # 该分集内 mp4 数量
                # 分集内部任务列表：一组合格素材 = 一个渲染任务
                "tasks": [
                    {
                        "name": group["name"],
                        "videoPath": group["videoPath"],
                        "audioPath": group["audioPath"],
                        "subtitlePath": group["subtitlePath"],
                    }
                    for group in groups
                ],
                "skipped": episode.get("skipped") or [],
            }
        )
    result["detail"] = detail
    result["summary"] = {
        "episodeTotal": len(detail),
        # 有效分集：至少含一组合格素材的分集
        "validEpisodeTotal": sum(1 for item in detail if item["audioCount"] > 0),
        "audioTotal": sum(item["audioCount"] for item in detail),
        "videoTotal": sum(item["videoCount"] for item in detail),
    }
    return result


@app.get("/api/compose/presets")
def presets_list() -> list[dict]:
    return models.list_presets()


@app.post("/api/compose/presets")
def presets_create(req: PresetRequest) -> dict:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="预设名称不能为空")
    preset = models.create_preset(name, req.config)
    if preset is None:
        raise HTTPException(status_code=500, detail="预设保存失败")
    return preset


@app.delete("/api/compose/presets/{preset_id}")
def presets_delete(preset_id: int) -> dict:
    if not models.delete_preset(preset_id):
        raise HTTPException(status_code=404, detail="预设不存在")
    return {"success": True, "id": preset_id}


@app.post("/api/compose/tasks")
def tasks_create(req: TaskRequest) -> dict:
    """创建批量合成任务：先落库，再交给并发渲染模块处理"""
    # 同 scan：空值/纯空白若放行会被 Path("") 误判为当前工作目录
    if not (req.sourceDir or "").strip():
        raise HTTPException(status_code=400, detail="处理来源目录不能为空")
    source_dir = Path(req.sourceDir).expanduser()
    if not source_dir.exists() or not source_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"根目录无效：{source_dir}")
    if req.bgm.enabled and not req.bgm.file:
        raise HTTPException(status_code=400, detail="已启用背景音乐，但未选择 BGM 文件")

    # 先扫描一次拿到分集总数（渲染阶段会再次扫描，保证与实际文件一致）
    try:
        scan_result = scan_episodes(source_dir)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    total = scan_result["total"]
    if total == 0:
        raise HTTPException(status_code=400, detail="该目录下没有找到任何分集子文件夹")

    # 存任务参数快照（含 sourceDir，供渲染层直接复用）
    config_snapshot = req.model_dump()
    task = models.create_task(
        source_dir=str(source_dir), total=total, config=config_snapshot, status="pending"
    )
    if task is None:
        raise HTTPException(status_code=500, detail="任务创建失败")

    if start_task_async is None:
        # 阶段6 之前渲染模块不存在：任务已落库，但不启动渲染
        models.update_task(task["id"], status="failed")
        models.append_task_log(task["id"], "[错误] 渲染模块未就绪（task_runner.py 尚未提供）")
        raise HTTPException(status_code=503, detail="渲染模块未就绪，无法开始合成")

    # 提交到线程池并发渲染，接口立即返回，前端通过轮询获取进度
    start_task_async(task["id"])
    # 返回体追加 elapsed/elapsedSeconds：点击【开始处理】的 created_at 即起始时间戳
    return _with_runtime(task)


@app.get("/api/compose/tasks/{task_id}")
def tasks_get(task_id: int) -> dict:
    """查询任务进度、成功/失败数量与完整日志"""
    task = models.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _with_runtime(task)


@app.get("/api/compose/tasks/{task_id}/progress")
def tasks_progress(task_id: int) -> dict:
    """查询全局进度 + 各分集独立进度与状态（前端轮询刷新进度条与分集表格）

    在任务记录的基础上追加：
      totalGroups / doneGroups  全部有效素材组数、已完成渲染的组数
      episodes                  各分集 { index, name, total, done, success, failed, status }
    其中 status：pending 待处理 / running 渲染中 / success 成功 / failed 失败
    """
    task = models.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    runtime = get_task_progress(task_id) if get_task_progress else None
    if runtime is None:
        # 尚未启动渲染（或后端中途重启）：返回空运行态，表格行保持「待处理」
        runtime = {"totalGroups": 0, "doneGroups": 0, "success": 0, "failed": 0, "episodes": []}

    # 用 _with_runtime 包裹：原有 progress/分集状态字段全部保留，
    # 仅追加 elapsed/elapsedSeconds（全局已运行时长），前端统计栏实时刷新用
    return _with_runtime(
        {
            **task,
            "totalGroups": runtime["totalGroups"],
            "doneGroups": runtime["doneGroups"],
            "episodes": runtime["episodes"],
        }
    )


@app.post("/api/compose/tasks/{task_id}/cancel")
def tasks_cancel(task_id: int) -> dict:
    """手动取消任务（协作式）：计时在取消时刻停止，终态为 canceled，不触发完成弹窗"""
    task = models.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") in ("completed", "failed", "canceled"):
        raise HTTPException(status_code=400, detail="任务已结束，无法取消")
    if request_cancel is None:
        raise HTTPException(status_code=503, detail="渲染模块未就绪，无法取消")

    if not request_cancel(task_id):
        raise HTTPException(status_code=400, detail="任务已处于取消状态")

    # 立即落库为终态 canceled：前端轮询据此停止计时并关闭进度更新（计时停止）
    models.update_task(task_id, status="canceled")
    models.append_task_log(task_id, "[取消] 已请求取消，正在停止后续渲染…")
    return _with_runtime(models.get_task(task_id))


# ---------------------------------------------------------------------------
# 本地启动入口（阶段7 由 Electron 拉起进程）
# ---------------------------------------------------------------------------

def _resolve_port() -> int:
    """端口优先级：命令行 --port > 环境变量 COMPOSE_PORT > 默认 8123"""
    argv = sys.argv[1:]
    if "--port" in argv:
        try:
            return int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            pass
    import os

    return int(os.environ.get("COMPOSE_PORT", DEFAULT_PORT))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=_resolve_port(), log_level="info")

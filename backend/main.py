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
    from task_runner import start_task_async
except ImportError:  # pragma: no cover - 阶段6 之前会走到这里
    start_task_async = None

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
    return task


@app.get("/api/compose/tasks/{task_id}")
def tasks_get(task_id: int) -> dict:
    """查询任务进度、成功/失败数量与完整日志"""
    task = models.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


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

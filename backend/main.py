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
from scanner import scan_projects

# 系统资源监控（零依赖 ctypes 实现）；不可用时接口返回「不支持」，不影响渲染流程
try:
    import system_monitor
except ImportError:  # pragma: no cover
    system_monitor = None

# 阶段6 提供的并发渲染入口；未就绪时给出明确提示而不是崩溃
try:
    from task_runner import (
        start_task_async,
        get_task_progress,
        request_cancel,
        get_cancel_at,
        set_project_paused,
        cancel_project,
    )
except ImportError:  # pragma: no cover - 阶段6 之前会走到这里
    start_task_async = None
    get_task_progress = None
    request_cancel = None
    get_cancel_at = None
    set_project_paused = None
    cancel_project = None

DEFAULT_PORT = 8123


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时建表（幂等）并启动系统资源采样线程（供页面资源监控面板读取）"""
    models.init_db()
    if system_monitor is not None:
        system_monitor.start()
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
    # 新增：支持批量加载多个一级项目根目录（每个元素 = 一个项目）
    sourceDirs: list[str] = Field(default_factory=list, description="一级项目根目录列表")
    # 兼容旧协议：单个根目录
    sourceDir: str = Field("", description="分集根目录（单个，兼容旧协议）")


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
    # 字体绝对路径（可选）：字体会被 libass 直接定位，省掉逐个字体目录查找的耗时，
    # 也避免「字体缺失 → 回退别的字体」造成的字幕渲染异常
    fontFile: str = ""    # 字体文件绝对路径（如 C:\Windows\Fonts\msyh.ttc）
    fontsDir: str = ""    # 字体所在目录（提供 fontFile 时可留空，自动取其所在目录）


class OutputConfig(BaseModel):
    mergeVideo: bool = True
    mergeWithFirst: bool = False
    width: int = 1280
    height: int = 720
    bitrate: int = 2000
    preset: str = "fast"      # h.264 编码预设：slow / medium / fast / veryfast
    crf: int = 0              # >0 时使用恒定质量 CRF，否则用 bitrate 目标码率
    engine: str = "auto"      # 编码引擎：auto / cpu / nvenc / qsv / amf（不可用自动降级 CPU）


class PerformanceConfig(BaseModel):
    parallelEnabled: bool = True
    maxWorkers: int = 4
    perfLimit: int = 80
    # 进程防护：单集/拼接的 ffmpeg 执行超时（秒，0=用默认 21600）与单进程内存上限（MB，0=不限）
    taskTimeoutSec: int = 21600
    memoryLimitMB: int = 0
    # 系统资源保护阈值：CPU 持续高于该值 / 磁盘吞吐高于该值 → 暂停提交新任务（0=不启用该判据）
    cpuThreshold: int = 90
    ioThresholdMBps: float = 0.0
    # 调度模式：multi 高性能多任务模式 / serial 低配单任务串行模式
    scheduleMode: str = "multi"
    # 调度策略：project_first 项目优先 / even 均分任务（串行模式下固定为项目优先）
    schedulePolicy: str = "project_first"


class ProjectConfig(BaseModel):
    """一级项目条目：根目录 + 该项自己的合并勾选

    合并按项目独立控制：项目 A 勾选只合并 A，项目 B 不勾选就不合并 B。
    """
    root: str = Field("", description="一级项目根目录")
    mergeVideo: bool = Field(False, description="创建合并视频（按分集顺序拼接）")
    mergeWithFirst: bool = Field(False, description="合并原视频与第一集（加原片头）")


class TaskRequest(BaseModel):
    # 新增：多项目列表（每项自带合并勾选，实现「按项目独立合并」）
    projects: list[ProjectConfig] = Field(default_factory=list)
    # 兼容旧协议：sourceDirs 列表 / sourceDir 单值（合并开关取 output.mergeVideo）
    sourceDirs: list[str] = Field(default_factory=list)
    sourceDir: str = ""
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


@app.get("/api/system/resources")
def system_resources() -> dict:
    """实时系统资源快照（页面资源监控面板每 2 秒轮询）

    返回 CPU 占用率、磁盘读写吞吐、是否处于饱和（饱和时后台会临时暂停提交新任务）。
    监控模块不可用时返回「不支持」占位，前端做降级展示。
    """
    if system_monitor is None:
        return {
            "supported": False,
            "cpuPercent": 0.0,
            "diskReadMBps": 0.0,
            "diskWriteMBps": 0.0,
            "processCount": 0,
            "cpuThreshold": 0.0,
            "ioThresholdMBps": 0.0,
            "cpuHigh": False,
            "ioHigh": False,
            "throttled": False,
        }
    return system_monitor.snapshot()


@app.post("/api/compose/scan")
def scan(req: ScanRequest) -> dict:
    """扫描一个或多个一级项目根目录（三层目录：项目 → 分集 → 视频/音频）"""
    # 空字符串/纯空白必须显式拦截：Path("") 等价于 Path(".")，
    # 会被扫描到后端进程的当前工作目录
    dirs = [str(item).strip() for item in (req.sourceDirs or []) if str(item or "").strip()]
    if not dirs and (req.sourceDir or "").strip():
        dirs = [req.sourceDir.strip()]
    if not dirs:
        raise HTTPException(status_code=400, detail="处理来源目录不能为空")
    try:
        result = scan_projects(dirs)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=f"没有访问权限：{e}")

    # 在扫描结果上追加三部分数据：
    #   detail          → 分集明细，供页面表格渲染（一行 = 一个二级分集文件夹）
    #   projectWarnings → 项目级告警（如某个分集缺【视频】/【音频】文件夹）
    #   summary         → 全局汇总，供页面【全局统计栏】渲染
    detail = []
    project_warnings = []
    for project in result["projects"]:
        for warning in project.get("warnings") or []:
            project_warnings.append(
                {
                    "projectIndex": project["index"],
                    "projectName": project["name"],
                    "text": warning,
                }
            )
        for episode in project["episodes"]:
            groups = episode["groups"]
            detail.append(
                {
                    # 复合键：多项目并存时，各项目的分集序号会重复，用「项目-分集」唯一标识
                    "key": f"{project['index']}-{episode['index']}",
                    "projectIndex": project["index"],
                    "projectName": project["name"],
                    "index": episode["index"],
                    "name": episode["name"],
                    "audioCount": len(groups),                   # 合格素材组数（mp4+mp3+srt 同名）
                    "videoCount": episode["videoCount"],          # 该分集内 mp4 数量
                    "pendingCount": episode["pendingCount"],      # 待渲染组数
                    "resumedCount": episode["resumedCount"],      # 成品已存在、断点续跑跳过数
                    # 分集内部任务列表：一组合格素材 = 一个渲染任务
                    "tasks": [
                        {
                            "name": group["name"],
                            "videoPath": group["videoPath"],
                            "audioPath": group["audioPath"],
                            "subtitlePath": group["subtitlePath"],
                            "outputPath": group["outputPath"],
                            "resumed": group["resumed"],
                        }
                        for group in groups
                    ],
                    "skipped": episode["skipped"],
                }
            )
    result["detail"] = detail
    result["projectWarnings"] = project_warnings
    result["summary"] = {
        **result["summary"],
        # 兼容旧字段名：audioTotal = 合格素材组总数
        "audioTotal": result["summary"].get("groupTotal", 0),
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
    # 项目列表解析：优先 projects（每项自带合并勾选，按项目独立控制合并），
    # 回退到旧协议 sourceDirs / sourceDir（此时合并开关取 output 配置）
    root_dirs = [
        str(item.root).strip() for item in (req.projects or []) if str(item.root or "").strip()
    ]
    if not root_dirs:
        root_dirs = [str(d).strip() for d in (req.sourceDirs or []) if str(d or "").strip()]
    if not root_dirs and (req.sourceDir or "").strip():
        root_dirs = [req.sourceDir.strip()]
    # 同 scan：空值/纯空白若放行会被 Path("") 误判为当前工作目录
    if not root_dirs:
        raise HTTPException(status_code=400, detail="处理来源目录不能为空")

    for root in root_dirs:
        path = Path(root).expanduser()
        if not path.exists() or not path.is_dir():
            raise HTTPException(status_code=400, detail=f"项目根目录无效：{path}")
    source_dir = Path(root_dirs[0]).expanduser()
    if req.bgm.enabled and not req.bgm.file:
        raise HTTPException(status_code=400, detail="已启用背景音乐，但未选择 BGM 文件")

    # 先扫描一次拿到分集总数（渲染阶段会再次扫描，保证与实际文件一致）
    try:
        scan_result = scan_projects(root_dirs)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    total = scan_result["summary"]["episodeTotal"]
    if total == 0:
        raise HTTPException(
            status_code=400,
            detail="所选项目下没有找到任何有效分集（二级分集内需同时存在【视频】【音频】文件夹）",
        )

    # 存任务参数快照（含项目列表，供渲染层直接复用）；
    # 同时把解析后的目录列表写回 sourceDirs，便于日志/排查与旧逻辑兜底
    config_snapshot = req.model_dump()
    config_snapshot["sourceDirs"] = root_dirs
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
      resumed / skippedGroups   断点续跑跳过的组数、项目被取消后跳过的组数
      episodes                  各分集 { key, projectIndex, projectName, index, name,
                                          total, done, success, failed, status }
      projects                  各项目 { index, name, paused, canceled, merged, total, done … }
    其中 status：pending 待处理 / running 渲染中 / success 成功 / failed 失败 / canceled 已取消
    """
    task = models.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    runtime = get_task_progress(task_id) if get_task_progress else None
    if runtime is None:
        # 尚未启动渲染（或后端中途重启）：返回空运行态，表格行保持「待处理」
        runtime = {
            "totalGroups": 0,
            "doneGroups": 0,
            "success": 0,
            "failed": 0,
            "resumed": 0,
            "skippedGroups": 0,
            "episodes": [],
            "projects": [],
        }

    # 用 _with_runtime 包裹：原有 progress/分集状态字段全部保留，
    # 仅追加 elapsed/elapsedSeconds（全局已运行时长），前端统计栏实时刷新用
    return _with_runtime(
        {
            **task,
            "totalGroups": runtime["totalGroups"],
            "doneGroups": runtime["doneGroups"],
            "resumed": runtime.get("resumed", 0),
            "skippedGroups": runtime.get("skippedGroups", 0),
            "episodes": runtime["episodes"],
            # 项目级状态：供页面按项目展示进度与「暂停/恢复/取消」按钮
            "projects": runtime.get("projects", []),
            # 全局双时长：仅用于新增的性能/并发面板展示，不影响原统计栏
            "renderSeconds": runtime.get("renderSeconds", 0),
            "renderText": runtime.get("renderText") or "00:00:00",
            "queueSeconds": runtime.get("queueSeconds", 0),
            "queueText": runtime.get("queueText") or "00:00:00",
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
# 项目级控制（多项目任务：单独暂停/恢复/取消某一个一级项目，不影响其他项目）
# ---------------------------------------------------------------------------

def _project_control_guard(task_id: int) -> dict:
    """项目级控制的公共校验：任务必须存在且仍在运行"""
    if set_project_paused is None or cancel_project is None:
        raise HTTPException(status_code=503, detail="渲染模块未就绪，无法控制项目")
    task = models.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") in ("completed", "failed", "canceled"):
        raise HTTPException(status_code=400, detail="任务已结束，无法控制项目")
    return task


@app.post("/api/compose/tasks/{task_id}/projects/{project_index}/pause")
def project_pause(task_id: int, project_index: int) -> dict:
    """暂停指定项目：未开始的素材组暂不提交，已开始的渲染正常跑完"""
    _project_control_guard(task_id)
    if not set_project_paused(task_id, project_index, True):
        raise HTTPException(status_code=400, detail="项目不存在或任务尚未开始渲染")
    models.append_task_log(
        task_id, f"[项目#{project_index}] 已暂停（未开始的任务暂不提交，已开始的渲染会跑完）", "WARN"
    )
    return {"success": True, "taskId": task_id, "projectIndex": project_index, "paused": True}


@app.post("/api/compose/tasks/{task_id}/projects/{project_index}/resume")
def project_resume(task_id: int, project_index: int) -> dict:
    """恢复指定项目：继续提交该项目剩余素材组"""
    _project_control_guard(task_id)
    if not set_project_paused(task_id, project_index, False):
        raise HTTPException(status_code=400, detail="项目不存在或任务尚未开始渲染")
    models.append_task_log(task_id, f"[项目#{project_index}] 已恢复，继续提交剩余任务", "INFO")
    return {"success": True, "taskId": task_id, "projectIndex": project_index, "paused": False}


@app.post("/api/compose/tasks/{task_id}/projects/{project_index}/cancel")
def project_cancel(task_id: int, project_index: int) -> dict:
    """取消指定项目：终止该项目所有未完成素材组（在跑的 ffmpeg 立即强杀），其他项目不受影响"""
    _project_control_guard(task_id)
    if not cancel_project(task_id, project_index):
        raise HTTPException(status_code=400, detail="项目不存在或已处于取消状态")
    models.append_task_log(
        task_id, f"[项目#{project_index}] 已取消，该项目剩余任务将跳过", "WARN"
    )
    return {"success": True, "taskId": task_id, "projectIndex": project_index, "canceled": True}


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

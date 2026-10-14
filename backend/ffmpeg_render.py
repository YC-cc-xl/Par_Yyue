"""FFmpeg 渲染模块

职责：
  1. 定位 ffmpeg.exe（程序目录优先，PATH 兜底）
  2. 单集渲染：视频 + 配音混音、BGM 无限循环叠加、SRT 字幕硬烧、缩放/码率
  3. 多集拼接（合并视频）
  4. 完整捕获 ffmpeg 的 stdout/stderr 作为日志

设计说明：
  - 字幕采用「SRT → ASS」转换后再烧制：样式直接写进 ASS 文件头，
    避免把中文字体名/中文路径塞进 ffmpeg 滤镜串导致的转义与编码问题；
    同时 ASS 提供字号、颜色、描边、位置的精确控制。
  - 转换后的 ASS 会写入临时工作目录（使用简单 ASCII 文件名），进一步规避路径转义风险。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

FFMPEG_BIN_NAME = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
FFPROBE_BIN_NAME = "ffprobe.exe" if os.name == "nt" else "ffprobe"

# 渲染超时（秒）：单集最长 6 小时，防止异常挂死
RENDER_TIMEOUT = 6 * 3600

# h.264 编码预设（越快渲染越快，画质轻微下降）；默认 fast
VIDEO_PRESETS = ("slow", "medium", "fast", "veryfast")
DEFAULT_PRESET = "fast"

# 音频统一格式：链首固定采样率/声道，避免混音阶段反复动态重采样带来的额外耗时
AUDIO_FORMAT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

# ---------------------------------------------------------------------------
# 硬件编码引擎
#   auto：按 NVENC(N卡) > QSV(Intel核显) > AMF(A卡) 优先级挑选可用硬件编码器，
#         均不可用时自动降级为 CPU libx264 软编码。
#   注意：硬烧字幕 subtitles 滤镜无法硬件加速，字幕叠加始终由 CPU 完成；
#         硬件加速仅作用于「视频编码」这一步。
# ---------------------------------------------------------------------------
ENGINE_ORDER = ("nvenc", "qsv", "amf")
ENGINE_CODECS = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf", "cpu": "libx264"}
ENGINE_LABELS = {"auto": "自动", "cpu": "CPU软编码", "nvenc": "N卡NVENC", "qsv": "Intel QSV", "amf": "A卡AMF"}

# 硬件编码默认 CQ（恒定质量，替代软编码 CRF）
DEFAULT_CQ = 23

# 页面原有编码预设 → 各引擎的 preset 取值（保留 slow/medium/fast/veryfast 下拉语义）
ENGINE_PRESETS: dict[str, dict[str, str]] = {
    "cpu": {"slow": "slow", "medium": "medium", "fast": "fast", "veryfast": "veryfast"},
    "nvenc": {"slow": "p6", "medium": "p4", "fast": "p3", "veryfast": "p2"},
    "qsv": {"slow": "slow", "medium": "medium", "fast": "fast", "veryfast": "veryfast"},
    "amf": {"slow": "quality", "medium": "balanced", "fast": "speed", "veryfast": "speed"},
}

_ffmpeg_cache: Optional[Path] = None


class FfmpegNotFound(RuntimeError):
    """未找到 ffmpeg 可执行文件"""


# ---------------------------------------------------------------------------
# 定位 ffmpeg
# ---------------------------------------------------------------------------

def locate_ffmpeg() -> Optional[Path]:
    """按优先级查找 ffmpeg 可执行文件

    1) 环境变量 COMPOSE_FFMPEG（阶段7 Electron 打包后注入程序目录）
    2) 项目/程序目录下的 ffmpeg/ffmpeg.exe（开发：项目根；打包：resources/ffmpeg）
    3) 系统 PATH
    """
    global _ffmpeg_cache
    if _ffmpeg_cache and _ffmpeg_cache.is_file():
        return _ffmpeg_cache

    backend_dir = Path(__file__).resolve().parent
    candidates: list[Path] = []

    env_path = os.environ.get("COMPOSE_FFMPEG")
    if env_path:
        candidates.append(Path(env_path))

    # backend 的上级目录：开发时为项目根目录，打包后为 resources 目录
    candidates.append(backend_dir.parent / "ffmpeg" / FFMPEG_BIN_NAME)
    candidates.append(backend_dir.parent / "resources" / "ffmpeg" / FFMPEG_BIN_NAME)

    which_path = shutil.which(FFMPEG_BIN_NAME) or shutil.which("ffmpeg")
    if which_path:
        candidates.append(Path(which_path))

    for candidate in candidates:
        if candidate.is_file():
            _ffmpeg_cache = candidate
            return candidate
    return None


def require_ffmpeg() -> Path:
    exe = locate_ffmpeg()
    if exe is None:
        raise FfmpegNotFound(
            "未找到 ffmpeg，请将 ffmpeg.exe 放到程序目录的 ffmpeg 文件夹下（或设置环境变量 COMPOSE_FFMPEG）"
        )
    return exe


# ---------------------------------------------------------------------------
# 硬件编码器探测（结果全局缓存，只探测一次）
#   - 先用 `ffmpeg -encoders` 拿到本机二进制【编译进】的编码器清单（1 次调用）
#   - 再对候选编码器逐个「真实试编码」：仅出现在清单里不代表本机有对应 GPU，
#     只有试编码成功（返回码 0）才算可用，避免误判
# ---------------------------------------------------------------------------

_ffprobe_cache: Optional[Path] = None
_hw_available: Optional[list[str]] = None
_hw_lock = threading.Lock()


def locate_ffprobe() -> Optional[Path]:
    """定位 ffprobe（与 ffmpeg 同目录优先；程序未随附时返回 None 并回退 ffmpeg -i）"""
    global _ffprobe_cache
    if _ffprobe_cache and _ffprobe_cache.is_file():
        return _ffprobe_cache

    candidates: list[Path] = []
    env_path = os.environ.get("COMPOSE_FFPROBE")
    if env_path:
        candidates.append(Path(env_path))
    ffmpeg_exe = locate_ffmpeg()
    if ffmpeg_exe is not None:
        candidates.append(ffmpeg_exe.with_name(FFPROBE_BIN_NAME))
    which_path = shutil.which(FFPROBE_BIN_NAME)
    if which_path:
        candidates.append(Path(which_path))

    for candidate in candidates:
        if candidate.is_file():
            _ffprobe_cache = candidate
            return candidate
    return None


def _compiled_encoders(ffmpeg: Path) -> set[str]:
    """读取 ffmpeg 二进制编译进的编码器名集合（失败返回空集合）"""
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    names: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def _trial_encode(ffmpeg: Path, codec: str) -> bool:
    """用 0.2s 黑白画面真实试编码一次，判断该硬件编码器在本机是否可用"""
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-v", "error",
             "-f", "lavfi", "-i", "color=black:s=256x144:d=0.2",
             "-an", "-c:v", codec, "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def detect_hardware_encoders(ffmpeg: Optional[Path] = None) -> list[str]:
    """探测本机可用的硬件编码器，按 NVENC > QSV > AMF 优先级返回（不可用返回空列表）

    结果全局缓存：整个进程生命周期只探测一次，不随每次渲染重复执行。
    """
    global _hw_available
    with _hw_lock:
        if _hw_available is not None:
            return list(_hw_available)

        ffmpeg = ffmpeg or locate_ffmpeg()
        available: list[str] = []
        if ffmpeg is not None:
            compiled = _compiled_encoders(ffmpeg)
            for engine in ENGINE_ORDER:
                if ENGINE_CODECS[engine] in compiled and _trial_encode(ffmpeg, ENGINE_CODECS[engine]):
                    available.append(engine)
        _hw_available = available
        return list(available)


def resolve_encoder(output: dict[str, Any], ffmpeg: Optional[Path] = None) -> tuple[str, str]:
    """把页面选择的编码引擎解析为 (引擎key, 编码器名)

    - auto：取优先级最高的可用硬件编码器，没有则 CPU
    - 指定硬件引擎但本机不可用：自动降级为 CPU libx264
    """
    engine = str(output.get("engine") or "auto").strip().lower()
    if engine not in ("auto", "cpu", *ENGINE_ORDER):
        engine = "auto"

    if engine == "cpu":
        return "cpu", ENGINE_CODECS["cpu"]

    available = detect_hardware_encoders(ffmpeg)
    if engine == "auto":
        engine = available[0] if available else "cpu"
    elif engine not in available:
        engine = "cpu"   # 指定引擎不可用 → 自动降级软编码
    return engine, ENGINE_CODECS[engine]


# ---------------------------------------------------------------------------
# 源素材信息预解析（ffprobe / ffmpeg -i）与内存缓存
#   扫描阶段一次性读取全部源视频/音频的分辨率、编码、采样率、通道数写入缓存，
#   后续构建 ffmpeg 命令直接读缓存，避免重复探测。
# ---------------------------------------------------------------------------

_media_cache: dict[str, dict[str, Any]] = {}

_RE_VIDEO_SIZE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_RE_VIDEO_CODEC = re.compile(r"Stream #\d+:\d+.*?Video:\s*([A-Za-z0-9_]+)")
_RE_AUDIO_CODEC = re.compile(r"Stream #\d+:\d+.*?Audio:\s*([A-Za-z0-9_]+)")
_RE_SAMPLE_RATE = re.compile(r"(\d{4,6})\s*Hz")
_RE_CHANNELS = re.compile(r"Hz,\s*([A-Za-z0-9_]+)")
_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.(\d{1,3})")


def _channel_count(label: str, fallback: str) -> int:
    """把声道描述（mono/stereo/5.1 等）换算为声道数"""
    text = (label or fallback or "").strip().lower()
    if text == "mono":
        return 1
    if text == "stereo":
        return 2
    if text.isdigit():
        return int(text)
    if text.startswith("5.1"):
        return 6
    if text.startswith("7.1"):
        return 8
    return 0


def _probe_key(path: Path) -> str:
    """缓存键：路径 + 文件大小 + 修改时间（素材被替换后自动失效）"""
    try:
        stat = path.stat()
        return f"{path}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        return str(path)


def _probe_with_ffprobe(ffprobe: Path, path: Path) -> dict[str, Any]:
    """用 ffprobe -show_streams(JSON) 读取素材信息"""
    proc = subprocess.run(
        [str(ffprobe), "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30,
    )
    data = json.loads(proc.stdout or "{}")
    info: dict[str, Any] = {"width": 0, "height": 0, "videoCodec": "", "audioCodec": "",
                            "sampleRate": 0, "channels": 0, "duration": 0.0}
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and not info["width"]:
            info["width"] = int(stream.get("width") or 0)
            info["height"] = int(stream.get("height") or 0)
            info["videoCodec"] = str(stream.get("codec_name") or "")
        elif stream.get("codec_type") == "audio" and not info["audioCodec"]:
            info["audioCodec"] = str(stream.get("codec_name") or "")
            info["sampleRate"] = int(stream.get("sample_rate") or 0)
            info["channels"] = int(stream.get("channels") or 0)
    try:
        info["duration"] = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        info["duration"] = 0.0
    return info


def _probe_with_ffmpeg(ffmpeg: Path, path: Path) -> dict[str, Any]:
    """程序未随附 ffprobe 时的回退：解析 `ffmpeg -i` 的 stderr 文本"""
    proc = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    info: dict[str, Any] = {"width": 0, "height": 0, "videoCodec": "", "audioCodec": "",
                            "sampleRate": 0, "channels": 0, "duration": 0.0}

    video_lines = [ln for ln in text.splitlines() if "Video:" in ln]
    if video_lines:
        match = _RE_VIDEO_CODEC.search(video_lines[0])
        if match:
            info["videoCodec"] = match.group(1)
        size = _RE_VIDEO_SIZE.search(video_lines[0])
        if size:
            info["width"], info["height"] = int(size.group(1)), int(size.group(2))

    audio_lines = [ln for ln in text.splitlines() if "Audio:" in ln]
    if audio_lines:
        match = _RE_AUDIO_CODEC.search(audio_lines[0])
        if match:
            info["audioCodec"] = match.group(1)
        rate = _RE_SAMPLE_RATE.search(audio_lines[0])
        if rate:
            info["sampleRate"] = int(rate.group(1))
        channel = _RE_CHANNELS.search(audio_lines[0])
        info["channels"] = _channel_count(channel.group(1) if channel else "", "")

    duration = _RE_DURATION.search(text)
    if duration:
        hour, minute, second, frac = duration.groups()
        info["duration"] = int(hour) * 3600 + int(minute) * 60 + int(second) + float(f"0.{frac}")
    return info


def probe_media(path: Path) -> dict[str, Any]:
    """读取单个源素材的分辨率/编码/采样率/通道（带内存缓存，探测失败返回空信息）"""
    key = _probe_key(path)
    cached = _media_cache.get(key)
    if cached is not None:
        return cached

    info: dict[str, Any] = {"width": 0, "height": 0, "videoCodec": "", "audioCodec": "",
                            "sampleRate": 0, "channels": 0, "duration": 0.0}
    try:
        if path.is_file():
            ffprobe = locate_ffprobe()
            if ffprobe is not None:
                info = _probe_with_ffprobe(ffprobe, path)
            else:
                ffmpeg = locate_ffmpeg()
                if ffmpeg is not None:
                    info = _probe_with_ffmpeg(ffmpeg, path)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        pass  # 探测失败不影响渲染：调用方按空信息处理

    with _hw_lock:
        _media_cache[key] = info
    return info


def probe_sources(paths: list[str | Path]) -> int:
    """批量预解析源素材并写入缓存（扫描阶段调用一次），返回本次新增探测的数量"""
    probed = 0
    for item in paths:
        path = Path(item)
        key = _probe_key(path)
        if key not in _media_cache:
            probe_media(path)
            probed += 1
    return probed


# ---------------------------------------------------------------------------
# 字幕：SRT → ASS
# ---------------------------------------------------------------------------

_TS_PATTERN = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")

# 字幕位置 → ASS Alignment（小键盘布局：2 底部居中 / 5 正中 / 8 顶部居中）
_ALIGNMENT = {"bottom": 2, "center": 5, "top": 8}


def read_text_any_encoding(path: Path) -> str:
    """按常见编码读取文本（字幕文件常见 UTF-8 / GBK / UTF-16）"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gbk", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _ts_to_ass(hour: str, minute: str, second: str, millis: str) -> str:
    """00:00:01,250 → 0:00:01.25（ASS 使用百分秒，2 位）"""
    centi = int(millis.ljust(3, "0")[:3]) // 10
    return f"{int(hour)}:{int(minute):02d}:{int(second):02d}.{centi:02d}"


def _parse_srt(text: str) -> list[tuple[str, str, str]]:
    """解析 SRT，返回 [(开始时间, 结束时间, 文本)]"""
    lines = [line.rstrip("\r") for line in text.splitlines()]
    cues: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        # 序号行（可能缺省）
        if lines[i].strip().isdigit():
            i += 1
        if i >= len(lines):
            break
        stamps = _TS_PATTERN.findall(lines[i])
        if len(stamps) < 2:
            i += 1
            continue
        start = _ts_to_ass(*stamps[0])
        end = _ts_to_ass(*stamps[1])
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(_escape_ass_text(lines[i].strip()))
            i += 1
        if text_lines:
            cues.append((start, end, "\\N".join(text_lines)))
    return cues


def _escape_ass_text(text: str) -> str:
    """ASS 文本转义：花括号会被当作样式覆盖标记"""
    return text.replace("{", "(").replace("}", ")")


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    """#RRGGBB → &HAABBGGRR（ASS 颜色为 BGR 顺序）"""
    value = (hex_color or "#FFFFFF").lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        value = "FFFFFF"
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}".upper()


def srt_to_ass(srt_text: str, style: dict[str, Any], width: int, height: int) -> str:
    """把 SRT 文本转换为带样式的 ASS 文本"""
    font_name = str(style.get("fontFamily") or "微软雅黑")
    font_size = int(style.get("fontSize") or 28)
    primary = _ass_color(str(style.get("color") or "#FFFFFF"))
    outline_color = _ass_color(str(style.get("outlineColor") or "#000000"))
    outline_width = int(style.get("outlineWidth") or 0)
    alignment = _ALIGNMENT.get(str(style.get("position") or "bottom"), 2)
    margin_v = max(10, int(height * 0.04))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{primary},{primary},{outline_color},&H00000000,0,0,0,0,100,100,0,0,1,{outline_width},1,{alignment},40,40,{margin_v},134

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues = "".join(
        f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n"
        for start, end, text in _parse_srt(srt_text)
    )
    return header + dialogues


def _escape_filter_path(path: Path) -> str:
    """转义 ffmpeg 滤镜参数中的路径（反斜杠、冒号、单引号、逗号、方括号）"""
    value = str(path).replace("\\", "/")
    value = value.replace(":", "\\:")
    value = value.replace("'", "\\'")
    value = value.replace(",", "\\,")
    value = value.replace("[", "\\[").replace("]", "\\]")
    return value


# ---------------------------------------------------------------------------
# 单集渲染
# ---------------------------------------------------------------------------

def _even(value: int) -> int:
    """libx264 要求宽高为偶数"""
    number = max(2, int(value))
    return number - (number % 2)


def _work_dir() -> Path:
    """临时工作目录（存放转换后的 ASS 与拼接清单文件）"""
    directory = Path(tempfile.gettempdir()) / "auto_compose_work"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _resolve_preset(output: dict[str, Any]) -> str:
    """读取编码预设，非法/缺省值回落到默认 fast"""
    preset = str(output.get("preset") or DEFAULT_PRESET).strip().lower()
    return preset if preset in VIDEO_PRESETS else DEFAULT_PRESET


def video_encode_args(
    output: dict[str, Any],
    threads: Optional[int] = None,
    ffmpeg: Optional[Path] = None,
) -> list[str]:
    """h.264 公共编码参数：编码引擎 + 预设 + 质量参数（+ 可选编码线程）

    - engine：auto / cpu / nvenc / qsv / amf（auto 自动挑选可用硬件编码器，不可用降级 CPU）
    - preset：保留页面 slow / medium / fast / veryfast 下拉，按引擎映射为各自 preset
    - 软编码：crf > 0 用 -crf 恒定质量，否则 -b:v 目标码率
    - 硬件编码：用 CQ 恒定质量（-cq / -global_quality / -rc cqp），crf 字段在此充当 CQ，默认 23
    - threads：单个 ffmpeg 进程的编码线程数（页面「性能上限」），None 交给 ffmpeg 自行决定
    """
    engine, codec = resolve_encoder(output, ffmpeg)
    preset = _resolve_preset(output)
    args = ["-c:v", codec, "-preset", ENGINE_PRESETS[engine][preset]]

    try:
        quality = int(output.get("crf") or 0)
    except (TypeError, ValueError):
        quality = 0

    if engine == "cpu":
        if quality > 0:
            args += ["-crf", str(max(0, min(quality, 51)))]
        else:
            args += ["-b:v", f"{int(output.get('bitrate') or 2000)}k"]
    else:
        # 硬件编码统一走 CQ 恒定质量（替代软编码 CRF）
        cq = max(0, min(quality or DEFAULT_CQ, 51))
        if engine == "nvenc":
            args += ["-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
        elif engine == "qsv":
            args += ["-global_quality", str(cq)]
        elif engine == "amf":
            args += ["-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq)]

    if threads:
        args += ["-threads", str(max(1, int(threads)))]
    return args


def build_render_command(
    ffmpeg: Path,
    video_path: Path,
    audio_path: Path,
    out_file: Path,
    config: dict[str, Any],
    ass_file: Optional[Path] = None,
    threads: Optional[int] = None,
) -> list[str]:
    """组装单集渲染的 ffmpeg 命令

    config 结构（来自前端 TaskRequest）：
      bgm.enabled / bgm.file / bgm.volume
      subtitle.enabled
      output.width / output.height / output.bitrate / output.preset / output.crf / output.engine
    threads: 单个 ffmpeg 进程可用的编码线程数（对应页面「性能上限」），None 表示交给 ffmpeg 自行决定
    """
    bgm = config.get("bgm") or {}
    subtitle = config.get("subtitle") or {}
    output = config.get("output") or {}

    width = _even(int(output.get("width") or 1280))
    height = _even(int(output.get("height") or 720))

    bgm_enabled = bool(bgm.get("enabled")) and bool(bgm.get("file"))
    subtitle_on = bool(subtitle.get("enabled")) and ass_file is not None

    # ---- 视频链 ----
    # 直接读扫描阶段写入的预解析缓存：源分辨率已等于目标时跳过 scale/pad（省掉一道高开销缩放），
    # 否则按目标分辨率缩放（保持比例 + 黑边补齐）；两种情况都不插入多余 format / colorspace 转换。
    media = probe_media(video_path)
    same_size = media.get("width") == width and media.get("height") == height
    if same_size:
        filters: list[str] = ["[0:v]setsar=1[vbase]"]
    else:
        filters = [
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[vbase]"
        ]
    if subtitle_on:
        # 硬烧字幕：指定 original_size（免去 libass 二次缩放）并关闭复杂字形重排（shaping=simple），
        # 降低字幕烧制的 CPU 开销
        filters.append(
            f"[vbase]subtitles=filename='{_escape_filter_path(ass_file)}'"
            f":original_size={width}x{height}:shaping=simple[vout]"
        )
        video_map = "[vout]"
    else:
        video_map = "[vbase]"

    # ---- 音频链：配音（不足补静音） + BGM（无限循环、降音量）----
    # 链首统一固定采样率/声道（AUDIO_FORMAT），避免混音时反复动态重采样
    filters.append(f"[1:a]{AUDIO_FORMAT}[a_voice0]")
    filters.append("[a_voice0]apad[a_voice]")
    audio_map = "[a_voice]"
    if bgm_enabled:
        volume = max(0.0, min(1.0, float(bgm.get("volume", 20)) / 100.0))
        filters.append(f"[2:a]{AUDIO_FORMAT},volume={volume:.4f}[a_bgm]")
        filters.append(
            "[a_voice][a_bgm]amix=inputs=2:duration=longest:"
            "dropout_transition=0:normalize=0[a_mix]"
        )
        audio_map = "[a_mix]"

    command: list[str] = [str(ffmpeg), "-y", "-hide_banner", "-nostdin"]
    command += ["-i", str(video_path)]
    command += ["-i", str(audio_path)]
    if bgm_enabled:
        # -stream_loop -1：BGM 无限循环，配合 -shortest 以视频时长为准
        command += ["-stream_loop", "-1", "-i", str(bgm.get("file"))]

    command += ["-filter_complex", ";".join(filters)]
    command += ["-map", video_map, "-map", audio_map]
    # 视频：编码引擎（auto 自动挑选硬件编码器，不可用降级 CPU）+ 预设 + CQ/CRF/码率 + 可选编码线程
    command += video_encode_args(output, threads, ffmpeg)
    command += [
        "-map_metadata", "-1",   # 丢弃源素材多余元数据（章节/标签等），减少文件写入开销
        "-pix_fmt", "yuv420p",
        # 音频：因含 apad/混音无法流复制，统一固定参数编码，避免运行时重采样
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-shortest",            # 以最短输入（即视频）为准结束
        "-movflags", "+faststart",
        str(out_file),
    ]
    return command


def _font_warnings(ffmpeg_log: str) -> list[str]:
    """从 ffmpeg 日志中识别字体【缺失/回退】告警（仅记录，不中断渲染）

    注意：libass 正常也会输出 `fontselect: (字体名, ...) -> 实际字体` 这样的成功行，
    因此这里只匹配明确的失败特征，避免误报。
    """
    lowered = ffmpeg_log.lower()
    failure_signs = (
        "fontselect: failed",             # 完全找不到字体
        "failed to find any fallback",    # 找不到任何回退字体
        "not found, selecting one more font",  # 指定字体缺少字形，触发回退
        "could not find font",            # DirectWrite/字体库查找失败
    )
    if any(sign in lowered for sign in failure_signs):
        return ["字幕字体可能缺失，已使用系统回退字体"]
    return []


# ---------------------------------------------------------------------------
# ffmpeg 进程执行与「立即强杀」
#   - 用 Popen 取代 subprocess.run：把进程句柄通过 on_spawn 交给调用方（任务是
#     否取消由 is_canceled 判定），使外部可以在取消瞬间直接终止 ffmpeg
#   - Windows 用 taskkill /T /F 连子进程一起杀，兜底 proc.kill()
# ---------------------------------------------------------------------------

def _remove_partial(path: Optional[Path]) -> None:
    """删除被中断渲染留下的半成品文件（不存在则忽略）"""
    if path is None:
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def terminate_process(proc: subprocess.Popen) -> None:
    """立即终止 ffmpeg 进程（Windows 连子进程一起杀）"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
    if proc.poll() is None:  # taskkill 不可用时的兜底
        try:
            proc.kill()
        except OSError:
            pass


def run_ffmpeg(
    command: list[str],
    on_spawn: Optional[Callable[[subprocess.Popen], None]] = None,
    on_release: Optional[Callable[[subprocess.Popen], None]] = None,
    is_canceled: Optional[Callable[[], bool]] = None,
    timeout: int = RENDER_TIMEOUT,
) -> tuple[str, str, Optional[int], Optional[str]]:
    """执行 ffmpeg，返回 (stdout, stderr, returncode, 执行异常标记)

    异常标记：None=正常结束 / "timeout"=超时被终止 / "canceled"=任务取消被强杀
    """
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        # 独立进程组：便于 taskkill /T 连同子进程一起回收
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
    registered = False
    try:
        if on_spawn is not None:
            on_spawn(proc)      # 交出句柄；若已取消，调用方会立刻杀它
            registered = True
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return out or "", err or "", proc.returncode, "timeout"
    finally:
        if registered and on_release is not None:
            on_release(proc)

    if is_canceled is not None and is_canceled():
        return out or "", err or "", proc.returncode, "canceled"
    return out or "", err or "", proc.returncode, None


def render_episode(
    episode: dict[str, Any],
    out_file: Path,
    config: dict[str, Any],
    work_sub_name: str = "sub.ass",
    threads: Optional[int] = None,
    on_spawn: Optional[Callable[[subprocess.Popen], None]] = None,
    on_release: Optional[Callable[[subprocess.Popen], None]] = None,
    is_canceled: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """渲染单个分集

    episode: scanner 返回的分集字典（videoPath / audioPath / subtitlePath）
    out_file: 输出视频路径
    config:   任务参数（bgm / subtitle / output）
    threads:  单个 ffmpeg 进程的编码线程数（页面「性能上限」）
    on_spawn / on_release / is_canceled: 进程句柄交出与取消判定（支持立即强杀）
    返回: {"success", "output", "error", "log", "command"}；被强杀时附加 canceled=True
    """
    try:
        ffmpeg = require_ffmpeg()
    except FfmpegNotFound as exc:
        return _fail(out_file, str(exc))

    video_path = Path(episode.get("videoPath") or "")
    audio_path = Path(episode.get("audioPath") or "")
    subtitle_path = Path(episode.get("subtitlePath") or "") if episode.get("subtitlePath") else None

    # ---- 前置校验：文件缺失直接失败并给出明确原因 ----
    if not video_path.is_file():
        return _fail(out_file, f"视频文件缺失：{video_path}")
    if not audio_path.is_file():
        return _fail(out_file, f"配音文件缺失：{audio_path}")

    subtitle_cfg = config.get("subtitle") or {}
    style = subtitle_cfg.get("style") or {}
    output_cfg = config.get("output") or {}
    width = _even(int(output_cfg.get("width") or 1280))
    height = _even(int(output_cfg.get("height") or 720))

    # ---- 字幕预处理：SRT → ASS，写入临时目录（简单 ASCII 文件名）----
    ass_file: Optional[Path] = None
    if subtitle_cfg.get("enabled"):
        if subtitle_path and subtitle_path.is_file():
            try:
                srt_text = read_text_any_encoding(subtitle_path)
                ass_text = srt_to_ass(srt_text, style, width, height)
                ass_file = _work_dir() / work_sub_name
                ass_file.write_text(ass_text, encoding="utf-8-sig")
            except OSError as exc:
                return _fail(out_file, f"字幕预处理失败：{exc}")
        else:
            # 该集没有字幕文件：按设置继续渲染，仅不烧字幕
            ass_file = None

    # ---- 执行渲染：先写同目录临时文件，渲染成功后再原子重命名为最终文件 ----
    #   失败/超时/取消时只删除临时文件，最终路径上永远不会残留损坏的半成品视频
    out_file.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件必须保留扩展名（ffmpeg 依据扩展名选择封装格式，`.part` 会导致无法识别 muxer）
    tmp_file = out_file.with_name(f"{out_file.stem}.part{out_file.suffix}")
    command = build_render_command(
        ffmpeg, video_path, audio_path, tmp_file, config, ass_file, threads
    )
    command_text = _format_command(command)

    try:
        stdout, stderr, returncode, exec_error = run_ffmpeg(
            command, on_spawn=on_spawn, on_release=on_release, is_canceled=is_canceled
        )
    except OSError as exc:
        _remove_partial(tmp_file)
        return _fail(out_file, f"ffmpeg 启动失败：{exc}", command_text)

    if exec_error == "timeout":
        _remove_partial(tmp_file)
        return _fail(out_file, f"ffmpeg 渲染超时（超过 {RENDER_TIMEOUT} 秒）", command_text)
    if exec_error == "canceled":
        # 任务取消被强杀：删除半成品，并标注 canceled 供调度层与真实失败区分
        _remove_partial(tmp_file)
        return {
            **_fail(out_file, "任务已取消（ffmpeg 已终止）", command_text),
            "canceled": True,
        }

    ffmpeg_log = stdout + "\n" + stderr
    warnings = _font_warnings(ffmpeg_log)

    if returncode != 0:
        # 取日志末尾若干行作为错误摘要，便于页面展示
        tail = "\n".join(line for line in stderr.strip().splitlines()[-8:])
        _remove_partial(tmp_file)
        return _fail(out_file, f"ffmpeg 退出码 {returncode}：{tail}", command_text, ffmpeg_log)

    if not tmp_file.is_file():
        _remove_partial(tmp_file)
        return _fail(out_file, "ffmpeg 正常退出但未生成输出文件", command_text, ffmpeg_log)

    # ---- 渲染成功：重命名临时文件为最终输出（同目录 os.replace 为原子操作）----
    try:
        os.replace(tmp_file, out_file)
    except OSError as exc:
        _remove_partial(tmp_file)
        return _fail(out_file, f"输出文件重命名失败：{exc}", command_text, ffmpeg_log)

    return {
        "success": True,
        "output": str(out_file),
        "error": "",
        "command": command_text,
        "log": ffmpeg_log,
        "warnings": warnings,
    }


def _fail(
    out_file: Path,
    error: str,
    command_text: str = "",
    ffmpeg_log: str = "",
) -> dict[str, Any]:
    return {
        "success": False,
        "output": str(out_file),
        "error": error,
        "command": command_text,
        "log": ffmpeg_log,
        "warnings": [],
    }


def _format_command(command: list[str]) -> str:
    """把命令格式化为可读字符串（含空格的参数加引号），便于日志排查"""
    return " ".join(f'"{part}"' if " " in part else part for part in command)


# ---------------------------------------------------------------------------
# 多集拼接（合并视频）
# ---------------------------------------------------------------------------

def _escape_concat_path(path: Path) -> str:
    """concat 清单文件中的路径转义：统一用正斜杠，单引号按 concat 规则转义为 '\\'' """
    value = str(path.resolve()).replace("\\", "/")
    return value.replace("'", "'\\''")


def _scale_filter(width: Optional[int], height: Optional[int]) -> Optional[str]:
    """生成「缩放到目标分辨率 + 黑边补齐」的滤镜串（与单集渲染保持一致）"""
    if not width or not height:
        return None
    target_w, target_h = _even(int(width)), _even(int(height))
    return (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )


def concat_videos(
    parts: list[Path],
    out_file: Path,
    reencode: bool = False,
    width: Optional[int] = None,
    height: Optional[int] = None,
    bitrate: Optional[int] = None,
    preset: Optional[str] = None,
    crf: Optional[int] = 0,
    engine: Optional[str] = None,
    on_spawn: Optional[Callable[[subprocess.Popen], None]] = None,
    on_release: Optional[Callable[[subprocess.Popen], None]] = None,
    is_canceled: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """用 concat 解复用器把多个视频首尾拼接

    parts 均由本模块渲染，编码参数一致，默认 `-c copy` 直接拷贝流（音视频均不重编码，速度最快）。
    reencode=True（拼接对象含编码参数不同的原始视频）时改为重编码，并按传入的
    width/height/bitrate（或 preset/crf/engine）统一编码参数，保证成片与页面配置一致。
    """
    try:
        ffmpeg = require_ffmpeg()
    except FfmpegNotFound as exc:
        return _fail(out_file, str(exc))

    existing = [p for p in parts if p.is_file()]
    if not existing:
        return _fail(out_file, "没有可拼接的视频分片")
    if len(existing) == 1:
        # 只有一集时无需拼接，直接作为合并结果
        out_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(existing[0], out_file)
        return {"success": True, "output": str(out_file), "error": "", "log": "仅一集，直接复制"}

    list_file = _work_dir() / "concat_list.txt"
    lines = [f"file '{_escape_concat_path(p)}'" for p in existing]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    # 与单集渲染一致：先写同目录临时文件（保留扩展名），拼接成功后再重命名为最终输出
    tmp_file = out_file.with_name(f"{out_file.stem}.part{out_file.suffix}")
    command = [
        str(ffmpeg), "-y", "-hide_banner", "-nostdin",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
    ]
    if reencode:
        # 与单集渲染保持一致的编码参数（编码引擎 + 预设 + CQ/CRF/码率），音频固定 48kHz 立体声
        command += video_encode_args(
            {"preset": preset, "crf": crf, "bitrate": bitrate, "engine": engine},
            ffmpeg=ffmpeg,
        )
        command += [
            "-map_metadata", "-1",   # 丢弃多余元数据，减少写入开销
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        ]
        # 统一到页面配置的分辨率（保持比例 + 黑边补齐），避免成片沿用片头原始分辨率
        target = _scale_filter(width, height)
        if target:
            command += ["-vf", target]
    else:
        # 直接流复制：音视频均不重编码（最高效路径）
        command += ["-c", "copy", "-map_metadata", "-1"]
    command += ["-movflags", "+faststart", str(tmp_file)]

    try:
        log_out, log_err, returncode, exec_error = run_ffmpeg(
            command, on_spawn=on_spawn, on_release=on_release, is_canceled=is_canceled
        )
    except OSError as exc:
        _remove_partial(tmp_file)
        return _fail(out_file, f"ffmpeg 启动失败：{exc}")

    if exec_error == "timeout":
        _remove_partial(tmp_file)
        return _fail(out_file, f"视频拼接超时（超过 {RENDER_TIMEOUT} 秒）")
    if exec_error == "canceled":
        _remove_partial(tmp_file)
        return {**_fail(out_file, "任务已取消（拼接已终止）"), "canceled": True}

    log = log_out + "\n" + log_err
    if returncode != 0 or not tmp_file.is_file():
        _remove_partial(tmp_file)
        tail = "\n".join(log_err.strip().splitlines()[-8:])
        return _fail(out_file, f"视频拼接失败（退出码 {returncode}）：{tail}", "", log)

    try:
        os.replace(tmp_file, out_file)
    except OSError as exc:
        _remove_partial(tmp_file)
        return _fail(out_file, f"输出文件重命名失败：{exc}", "", log)

    return {"success": True, "output": str(out_file), "error": "", "log": log}

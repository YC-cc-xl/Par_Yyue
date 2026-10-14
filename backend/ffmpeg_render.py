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

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

FFMPEG_BIN_NAME = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

# 渲染超时（秒）：单集最长 6 小时，防止异常挂死
RENDER_TIMEOUT = 6 * 3600

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
      output.width / output.height / output.bitrate
    threads: 单个 ffmpeg 进程可用的编码线程数（对应页面「性能上限」），None 表示交给 ffmpeg 自行决定
    """
    bgm = config.get("bgm") or {}
    subtitle = config.get("subtitle") or {}
    output = config.get("output") or {}

    width = _even(int(output.get("width") or 1280))
    height = _even(int(output.get("height") or 720))
    bitrate = int(output.get("bitrate") or 2000)

    bgm_enabled = bool(bgm.get("enabled")) and bool(bgm.get("file"))
    subtitle_on = bool(subtitle.get("enabled")) and ass_file is not None

    # ---- 视频链：缩放到目标分辨率（保持比例 + 黑边补齐）----
    filters: list[str] = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[vbase]"
    ]
    if subtitle_on:
        filters.append(
            f"[vbase]subtitles=filename='{_escape_filter_path(ass_file)}'[vout]"
        )
        video_map = "[vout]"
    else:
        video_map = "[vbase]"

    # ---- 音频链：配音（不足补静音） + BGM（无限循环、降音量）----
    filters.append("[1:a]apad[a_voice]")
    audio_map = "[a_voice]"
    if bgm_enabled:
        volume = max(0.0, min(1.0, float(bgm.get("volume", 20)) / 100.0))
        filters.append(f"[2:a]volume={volume:.4f}[a_bgm]")
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
    command += ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{bitrate}k"]
    if threads:
        # 限制编码线程数，对应页面「性能上限」，防止并发渲染时 CPU 过载
        command += ["-threads", str(max(1, int(threads)))]
    command += [
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
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


def render_episode(
    episode: dict[str, Any],
    out_file: Path,
    config: dict[str, Any],
    work_sub_name: str = "sub.ass",
    threads: Optional[int] = None,
) -> dict[str, Any]:
    """渲染单个分集

    episode: scanner 返回的分集字典（videoPath / audioPath / subtitlePath）
    out_file: 输出视频路径
    config:   任务参数（bgm / subtitle / output）
    threads:  单个 ffmpeg 进程的编码线程数（页面「性能上限」）
    返回: {"success", "output", "error", "log", "command"}
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

    # ---- 执行渲染 ----
    out_file.parent.mkdir(parents=True, exist_ok=True)
    command = build_render_command(
        ffmpeg, video_path, audio_path, out_file, config, ass_file, threads
    )
    command_text = _format_command(command)

    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RENDER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _fail(out_file, f"ffmpeg 渲染超时（超过 {RENDER_TIMEOUT} 秒）", command_text)
    except OSError as exc:
        return _fail(out_file, f"ffmpeg 启动失败：{exc}", command_text)

    ffmpeg_log = (process.stdout or "") + "\n" + (process.stderr or "")
    warnings = _font_warnings(ffmpeg_log)

    if process.returncode != 0:
        # 取日志末尾若干行作为错误摘要，便于页面展示
        tail = "\n".join(
            line for line in (process.stderr or "").strip().splitlines()[-8:]
        )
        return _fail(out_file, f"ffmpeg 退出码 {process.returncode}：{tail}", command_text, ffmpeg_log)

    if not out_file.is_file():
        return _fail(out_file, "ffmpeg 正常退出但未生成输出文件", command_text, ffmpeg_log)

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
) -> dict[str, Any]:
    """用 concat 解复用器把多个视频首尾拼接

    parts 均由本模块渲染，编码参数一致，默认 `-c copy` 直接拷贝流（速度快）。
    reencode=True（拼接对象含编码参数不同的原始视频）时改为重编码，并按传入的
    width/height/bitrate 统一成目标分辨率与码率，保证成片与页面配置一致。
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
    command = [
        str(ffmpeg), "-y", "-hide_banner", "-nostdin",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
    ]
    if reencode:
        command += [
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
        ]
        # 统一到页面配置的分辨率（保持比例 + 黑边补齐），避免成片沿用片头原始分辨率
        target = _scale_filter(width, height)
        if target:
            command += ["-vf", target]
        if bitrate:
            command += ["-b:v", f"{int(bitrate)}k"]
    else:
        command += ["-c", "copy"]
    command += ["-movflags", "+faststart", str(out_file)]

    try:
        process = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=RENDER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _fail(out_file, f"视频拼接超时（超过 {RENDER_TIMEOUT} 秒）")
    except OSError as exc:
        return _fail(out_file, f"ffmpeg 启动失败：{exc}")

    log = (process.stdout or "") + "\n" + (process.stderr or "")
    if process.returncode != 0 or not out_file.is_file():
        tail = "\n".join((process.stderr or "").strip().splitlines()[-8:])
        return _fail(out_file, f"视频拼接失败（退出码 {process.returncode}）：{tail}", "", log)

    return {"success": True, "output": str(out_file), "error": "", "log": log}

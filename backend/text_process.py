"""文本处理引擎（纯函数，本地离线运行，不联网、不调用任何外部大模型）

三个对外函数（均为无副作用纯函数：输入文本 → 输出文本 + 统计字典）：
  clean_ads(text)                  → (清洗后文本, 统计)
  format_text(text, drop_chapter)  → (格式化后文本, 统计)
  split_by_length(text, size)      → (第一段, 剩余文本)

设计要点：
  1. 判定按「行」进行：广告行只在行较短时判删，避免把正文叙述整句误删；
  2. 换行统一归一化为 \\n，保证前端「第 X 行」定位与后端统计一致；
  3. 每一步都记录命中次数，前端日志展示「删了多少广告行 / 合并了多少断行」。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 通用字符类 / 正则
# ---------------------------------------------------------------------------

# 中日韩表意文字（用于判断「中文语境」，据此修正中英混排标点）
_CJK = r"\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af"

# 换行归一化：CRLF / CR → LF
_NEWLINE_RE = re.compile(r"\r\n|\r")

# 宽空白（制表符 / 全角空格 / 不间断空格等）→ 统一为半角空格
_WIDE_SPACE_RE = re.compile(r"[\t\u3000\u00a0\u200b\x0b\x0c]+")
# 连续半角空格 → 单个
_MULTI_SPACE_RE = re.compile(r" {2,}")

# HTML 残留标签（正文里的 <p> <br/> <div ...> 之类）
_HTML_TAG_RE = re.compile(r"<[^<>]{1,200}>")

# HTML 转义字符：&nbsp; &amp; &#160; &#x26; 等
_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]{2,10}|#\d{2,7}|#x[0-9a-fA-F]{2,6});")
_ENTITY_MAP = {
    "&nbsp;": " ",
    "&emsp;": " ",
    "&ensp;": " ",
    "&thinsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&apos;": "'",
    "&ldquo;": "“",
    "&rdquo;": "”",
    "&lsquo;": "‘",
    "&rsquo;": "’",
    "&hellip;": "…",
    "&mdash;": "—",
    "&ndash;": "–",
}

# ---------------------------------------------------------------------------
# 广告 / 引流内容识别
# ---------------------------------------------------------------------------

# 第一档：整行命中即删除（这些短语几乎只出现在广告行里）
_AD_STRONG_PHRASES = (
    "本章未完",
    "未完待续",
    "点击下一页",
    "下一页继续",
    "继续阅读",
    "请记住本站",
    "记住本站",
    "本书首发",
    "首发于",
    "首发网站",
    "最新章节",
    "无弹窗",
    "全文阅读",
    "手机阅读",
    "手机版阅读",
    "客户端下载",
    "下载客户端",
    "下载app",
    "关注公众号",
    "微信公众号",
    "公众号搜索",
    "微信搜索",
    "关注微信",
    "微信号",
    "书友群",
    "读者群",
    "交流群",
    "群号",
    "商务合作",
    "广告合作",
    "联系作者",
    "作者的话",
    "作者有话说",
    "求收藏",
    "求推荐",
    "求订阅",
    "求月票",
    "求打赏",
    "求鲜花",
    "求票票",
    "求票",
    "投推荐票",
    "投月票",
    "推荐票",
    "打赏",
    "催更",
    "加更",
    "免责声明",
    "版权声明",
    "版权所有",
    "如有侵权",
    "更新时间",
    "今日更新",
    "努力更新中",
    "本章完毕",
    "本章结束",
    "扫码",
    "二维码",
    # 常见盗版站关键词
    "笔趣",
    "顶点小说",
    "飘天文学",
    "番茄小说",
    "爱奇电子书",
    "无错小说",
    "新笔趣",
)

# 第二档：仅当该行很短时才判删（这些词本身也可能是正文用词，如「他打开微信」）
_AD_WEAK_PHRASES = (
    "广告",
    "推广",
    "收藏",
    "订阅",
    "点赞",
    "分享",
    "公众号",
    "微信",
    "网址",
    "网站",
    "手机版",
    "客户端",
    "下载",
)
_AD_WEAK_MAX_LEN = 20

# 网址 / 裸域名：行内出现即视为引流行
_URL_RE = re.compile(r"(?:https?://|www\.)\S{3,}", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9\-]{1,20}\.(?:com|net|cn|org|cc|info|xyz|top|vip|me|tv|biz|la|site)\b",
    re.IGNORECASE,
)

# 纯装饰分隔符行：整行只由分隔符号组成（如 ***、---、#####、☆☆☆、◇◇◇）
_DECOR_CHARS = r"*\-—_=~#☆◇★◆✦✧❖·•※"
_DECOR_ONLY_RE = re.compile(rf"^[{_DECOR_CHARS}]{{2,}}$")
# 行内连续 3 个以上的装饰符号（如「***」这种夹在正文里的分隔）
_DECOR_RUN_RE = re.compile(rf"[{_DECOR_CHARS}]{{3,}}")

# ---------------------------------------------------------------------------
# 章节标题识别（格式化时可选择删除；合并断行时用于判断「下一行不是新章节」）
#   拆成三条规则，避免「正文一」「正文二」这类正常段落被当成章节误删：
#     1) 编号章节：第1章 / 第 12 节 / 第三章 …
#     2) 独立标题词：楔子 / 番外篇 …（要求整行就是标题词，后面只能跟分隔符）
#     3) 英文章节：Chapter 12 …
# ---------------------------------------------------------------------------

_CHAPTER_NUM_RE = re.compile(
    r"^\s*第\s*[0-9０-９一二三四五六七八九十百千万零两]{1,8}\s*[章节回卷部篇集话]"
)
_CHAPTER_WORD_RE = re.compile(
    r"^\s*(?:楔子|序章|引子|尾声|后记|终章|大结局|外传|番外篇?|前言|简介|附录)"
    r"\s*(?:[：:、\-—.·].*)?$"
)
_CHAPTER_EN_RE = re.compile(r"^\s*chapter\s*\d+", re.IGNORECASE)

# ---------------------------------------------------------------------------
# 标点修正
# ---------------------------------------------------------------------------

# 半角标点 → 全角对照表（不含 < >：半角尖括号在正文里几乎都是 HTML 残留，改成《》反而弄坏原文）
_PUNCT_MAP = {
    ",": "，",
    ";": "；",
    ":": "：",
    "?": "？",
    "!": "！",
    "(": "（",
    ")": "）",
}

# 中文语境中的半角标点 → 全角（标点前后至少一侧是中日韩字符；
# 允许标点两侧存在多余空格，替换时一并吃掉空格，实现「句子内多余空白清除」）
# 负向后顾 (?<![A-Za-z0-9]) 用于跳过 HTML 实体（&nbsp; &amp; 等）与英文缩写里的标点
_MIXED_PUNCT_RE = re.compile(
    rf"(?<![A-Za-z0-9])[ \t]*[,;:?!()][ \t]*(?=[{_CJK}])"
    rf"|(?<=[{_CJK}])[ \t]*[,;:?!()'\"][ \t]*"
)
# 行尾的半角句点（前面是中文时）→ 中文句号（含「..」「...」这类重复半角点）
_TAIL_DOT_RE = re.compile(rf"(?<=[{_CJK}])\.+[ \t]*$", re.MULTILINE)
# 重复的中文标点（「，，」「。。」）→ 合并为一个
_DUP_PUNCT_RE = re.compile(r"([，。！？；：、])\1+")
# 行尾多余的逗号 / 顿号（合并断行之后仍然残留 → 属于多余标点）
_TAIL_COMMA_RE = re.compile(r"[，,、]+\s*$")
# 行尾为逗号 / 顿号 / 破折号 → 说明句子被错误换行截断，需要与下一行合并
_MERGE_TAIL_RE = re.compile(r"[，,、\-—–]+\s*$")

# 句子结束标点（剪切分段时优先在这些位置断开，避免把句子切碎）
_SENTENCE_TAIL_CHARS = "。！？…；」』）】》”"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def normalize_newlines(text: str) -> str:
    """把 CRLF / CR 统一成 LF（前后端行号一致的前提）"""
    return _NEWLINE_RE.sub("\n", text or "")


def _entity_repl(match: re.Match) -> str:
    """HTML 转义字符还原：已知实体还原为字符（&nbsp; → 空格），未知实体直接删除"""
    raw = match.group(0)
    key = raw.lower()
    if key in _ENTITY_MAP:
        return _ENTITY_MAP[key]
    code = raw[1:-1]
    try:
        if code[:2].lower() == "#x":
            return chr(int(code[2:], 16))
        if code.startswith("#"):
            return chr(int(code[1:]))
    except (ValueError, OverflowError):
        return ""
    return ""


def _collapse_wide_spaces(text: str) -> tuple[str, int]:
    """制表符 / 全角空格 / 连续空格 → 单个半角空格；返回 (文本, 命中次数)"""
    text, n1 = _WIDE_SPACE_RE.subn(" ", text)
    text, n2 = _MULTI_SPACE_RE.subn(" ", text)
    return text, n1 + n2


def _is_chapter_line(line: str) -> bool:
    """是否为章节标题行（第1章 xxx / 第一章 xxx / 楔子 / Chapter 12 …）"""
    text = line or ""
    return bool(
        _CHAPTER_NUM_RE.match(text)
        or _CHAPTER_WORD_RE.match(text)
        or _CHAPTER_EN_RE.match(text)
    )


def _is_decor_only(line: str) -> bool:
    """整行是否只由装饰/分隔符号组成（***、---、#####、☆☆☆ …）"""
    return bool(_DECOR_ONLY_RE.match(line or ""))


def _is_ad_line(line: str) -> bool:
    """是否为广告 / 引流 / 推广行

    两级判定：
      1. 命中强广告短语（几乎只出现在广告里的长短语）→ 直接判删；
      2. 含网址/裸域名 → 判删；
      3. 命中泛化广告词，且整行长度不超过阈值 → 判删（短行通常是广告条，长行更可能是正文叙述）。
    """
    if not line:
        return False
    if _URL_RE.search(line) or _DOMAIN_RE.search(line):
        return True
    if any(phrase in line for phrase in _AD_STRONG_PHRASES):
        return True
    if len(line) <= _AD_WEAK_MAX_LEN and any(word in line for word in _AD_WEAK_PHRASES):
        return True
    return False


def _fix_mixed_punct(line: str) -> tuple[str, int]:
    """修正中英混排标点：中文语境里的半角标点 → 全角（并吃掉标点两侧多余空格）"""
    count = 0

    def repl(match: re.Match) -> str:
        nonlocal count
        raw = match.group(0)
        ch = raw.strip()
        mapped = _PUNCT_MAP.get(ch, ch)
        if mapped != raw:
            count += 1
        return mapped

    return _MIXED_PUNCT_RE.sub(repl, line), count


# ---------------------------------------------------------------------------
# 1. 智能清理广告
# ---------------------------------------------------------------------------

def clean_ads(text: str) -> tuple[str, dict[str, int]]:
    """智能清理广告 / 垃圾内容，保留正文与章节标题

    规则（全部在本地离线执行）：
      1. 删除网页/网文广告行（【本章未完】、广告链接、推广话术、求票、打赏、公众号/微信/平台引流）；
      2. 连续空行清除（多行空白合并为最多 1 个换行）；
      3. 清除多余空格、制表符、全角/半角空白；
      4. 移除 *** / --- / 连续 # / ☆ / ◇ 等装饰分隔符；
      5. 过滤 HTML 残留标签与 &nbsp; 等转义字符；
      6. 只剔除垃圾内容，正文与章节标题全部保留。
    """
    stats = {
        "removedLines": 0,      # 删除的广告/垃圾行数
        "removedDecor": 0,      # 移除的装饰分隔符行（含行内装饰段）数
        "removedTags": 0,       # 移除的 HTML 标签数
        "removedEntities": 0,   # 处理的转义字符数
        "collapsedBlank": 0,    # 合并掉的连续空行段数
        "trimmedSpaces": 0,     # 清理的空白/制表符次数
    }
    if not text:
        return "", stats

    s = normalize_newlines(text)

    # 1) HTML 残留标签 + 转义字符
    s, n = _HTML_TAG_RE.subn("", s)
    stats["removedTags"] = n
    s, n = _ENTITY_RE.subn(_entity_repl, s)
    stats["removedEntities"] = n

    # 2) 制表符 / 全角空格 / 连续空格收敛
    s, stats["trimmedSpaces"] = _collapse_wide_spaces(s)

    # 3) 逐行过滤：空行合并、装饰行删除、广告行删除、行内装饰段清除
    kept: list[str] = []
    blank_run = 0
    for raw in s.split("\n"):
        line = raw.strip()
        if not line:
            blank_run += 1
            continue
        if blank_run:
            # 一段连续空行被合并为「无空行」（相邻保留行之间只剩 1 个换行）
            stats["collapsedBlank"] += 1
            blank_run = 0
        if _is_decor_only(line):
            stats["removedDecor"] += 1
            continue
        if _is_ad_line(line):
            stats["removedLines"] += 1
            continue
        # 行内连续装饰符号（夹在正文里的 *** 之类）直接剔除
        line, n = _DECOR_RUN_RE.subn("", line)
        if n:
            stats["removedDecor"] += n
            line = line.strip()
            if not line:
                continue
        kept.append(line)
    if blank_run:
        stats["collapsedBlank"] += 1

    return "\n".join(kept), stats


# ---------------------------------------------------------------------------
# 2. 智能格式化文本
# ---------------------------------------------------------------------------

def format_text(text: str, drop_chapter: bool = False) -> tuple[str, dict[str, int]]:
    """智能格式化：段落规整 → 标点修正 → 合并被错误换行切断的长句

    执行顺序：
      1. 【可选】删除章节标题行（勾选「格式化时删除章节名称」时执行）；
      2. 段落规整：一个段落只保留单行，段间仅一个换行，句子内多余空白清除；
      3. 修正标点：统一中英混排标点、合并重复标点、去除行尾多余逗号顿号；
      4. 合并断行：行尾是逗号/顿号/破折号且下一行不是新章节 → 合并到同一行。
    """
    stats = {
        "removedChapter": 0,   # 删除的章节标题行数
        "mergedLines": 0,      # 合并的断行次数
        "fixedPunct": 0,       # 修正的标点数（中英混排 + 重复标点）
        "trimmedTail": 0,      # 去除的行尾多余逗号/顿号数
    }
    if not text:
        return "", stats

    s = normalize_newlines(text)

    # ---- 段落规整：逐行 trim + 丢弃空行（段间只保留 1 个换行） ----
    lines: list[str] = []
    for raw in s.split("\n"):
        line, _ = _collapse_wide_spaces(raw.strip())
        if line:
            lines.append(line)

    # ---- 步骤 1：可选删除章节标题行 ----
    if drop_chapter:
        rest: list[str] = []
        for line in lines:
            if _is_chapter_line(line):
                stats["removedChapter"] += 1
                continue
            rest.append(line)
        lines = rest

    # ---- 步骤 2：标点修正（中英混排 + 重复标点 + 行尾半角句点） ----
    fixed: list[str] = []
    for line in lines:
        line, n1 = _fix_mixed_punct(line)
        line, n2 = _DUP_PUNCT_RE.subn(r"\1", line)
        line, n3 = _TAIL_DOT_RE.subn("。", line)
        stats["fixedPunct"] += n1 + n2 + n3
        fixed.append(line)

    # ---- 步骤 3：合并被换行错误切割的长句 ----
    merged: list[str] = []
    for line in fixed:
        # 上一行以逗号/顿号/破折号结尾，且当前行不是新章节 → 拼到上一行
        if merged and not _is_chapter_line(line) and _MERGE_TAIL_RE.search(merged[-1]):
            merged[-1] = merged[-1] + line
            stats["mergedLines"] += 1
        else:
            merged.append(line)

    # ---- 步骤 4：合并之后仍然残留的行尾逗号/顿号 → 视为多余标点剔除 ----
    cleaned: list[str] = []
    for line in merged:
        line, n = _TAIL_COMMA_RE.subn("", line)
        stats["trimmedTail"] += n
        cleaned.append(line)

    return "\n".join(cleaned), stats


# ---------------------------------------------------------------------------
# 3. 按字数剪切分段
# ---------------------------------------------------------------------------

def split_by_length(text: str, size: int) -> tuple[str, str]:
    """按字数把整篇文本切成「第一段 + 剩余文本」

    为避免把句子切碎，断点优先取「size 附近 300 字窗口内」的换行处，
    其次取句子结束标点处，都找不到才按字数硬切。
    返回 (第一段, 剩余文本)；剩余文本会去掉开头的空行，避免编辑器顶部空一大截。
    """
    size = int(size)
    if size <= 0:
        raise ValueError("剪切字数必须大于 0")

    s = normalize_newlines(text or "")
    if len(s) <= size:
        return s, ""

    cut = size
    window_start = max(0, size - 300)
    window = s[window_start:size]

    pos_nl = window.rfind("\n")
    if pos_nl >= 0:
        cut = window_start + pos_nl + 1
    else:
        for i in range(len(window) - 1, -1, -1):
            if window[i] in _SENTENCE_TAIL_CHARS:
                cut = window_start + i + 1
                break

    return s[:cut], s[cut:].lstrip("\n")

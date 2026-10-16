// 文本查找/定位纯函数集合（全部在前端本地执行，不联网）
// 设计目标：13 万字（130000 字）量级下保持流畅——
//   · 行索引只扫描一次，之后全部走二分查找
//   · 匹配使用原生 String.indexOf 循环（比正则快且无回溯风险）
//   · 替换用「一次性切片拼接」，不在循环里反复改动整串

/**
 * 统一换行符：把 \r\n、\r 全部归一化为 \n
 * 必要性：残留的 \r 会让「第 X 列」计算结果偏移 1，也会干扰按行处理
 */
export function normalizeNewlines(text) {
  return String(text == null ? '' : text).replace(/\r\n/g, '\n').replace(/\r/g, '\n')
}

/**
 * 构建行起始偏移索引
 * 返回数组 starts：starts[i] = 第 i 行（从 0 开始计）的首字符在全文中的偏移
 * 例："ab\ncd" → [0, 3]
 */
export function buildLineIndex(text) {
  const starts = [0]
  const len = text.length
  for (let i = 0; i < len; i++) {
    if (text.charCodeAt(i) === 10) starts.push(i + 1)
  }
  return starts
}

/**
 * 二分查找某偏移所属的行号（0 基）
 * 找满足 starts[i] <= offset 的最大 i
 */
export function lineIndexAt(starts, offset) {
  let lo = 0
  let hi = starts.length - 1
  let ans = 0
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (starts[mid] <= offset) {
      ans = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return ans
}

/**
 * 偏移 → 行列（均为 1 基，供状态栏「第 X 行，第 X 列」展示）
 */
export function offsetToLineCol(starts, offset) {
  const safe = Math.max(0, Math.min(offset, starts[starts.length - 1] + 1))
  const lineNo = lineIndexAt(starts, safe)
  return { line: lineNo + 1, col: safe - starts[lineNo] + 1 }
}

/**
 * 找出全部匹配偏移
 * limit：安全上限，防止极端输入（如全文只有一个逗号）把内存撑爆
 */
export function findAll(text, needle, limit = 200000) {
  const out = []
  if (!needle) return out
  const step = Math.max(1, needle.length)
  let idx = text.indexOf(needle)
  while (idx !== -1 && out.length < limit) {
    out.push(idx)
    idx = text.indexOf(needle, idx + step)
  }
  return out
}

/**
 * 替换单处：按偏移切片拼接（只影响这一处）
 */
export function replaceOneByOffset(text, offset, needle, replacement) {
  if (offset < 0 || offset + needle.length > text.length) return text
  return text.slice(0, offset) + replacement + text.slice(offset + needle.length)
}

/**
 * 替换多处：一次性切片拼接，比循环内字符串拼接快得多
 * matches 必须是从小到大排列的匹配起点数组
 */
export function replaceAllByOffsets(text, matches, needle, replacement) {
  if (!matches || !matches.length) return text
  const step = needle.length
  const parts = []
  let last = 0
  for (const offset of matches) {
    parts.push(text.slice(last, offset), replacement)
    last = offset + step
  }
  parts.push(text.slice(last))
  return parts.join('')
}

/**
 * 取某行文本（用于左侧搜索结果面板的「所在内容」列）
 * lineNo 为 0 基行号；maxLen 控制单行展示长度，超出截断
 */
export function lineTextAt(text, starts, lineNo, maxLen = 200) {
  if (lineNo < 0 || lineNo >= starts.length) return ''
  const from = starts[lineNo]
  const to = lineNo + 1 < starts.length ? starts[lineNo + 1] - 1 : text.length
  const raw = text.slice(from, to)
  return raw.length > maxLen ? `${raw.slice(0, maxLen)}…` : raw
}

/**
 * 从当前光标起找「下一个」匹配（循环查找：到文末自动回到开头）
 * 返回匹配序号（在 matches 中的下标）；没有匹配返回 -1
 */
export function nextMatchIndex(matches, fromOffset) {
  if (!matches || !matches.length) return -1
  const idx = matches.findIndex((m) => m > fromOffset)
  return idx === -1 ? 0 : idx
}

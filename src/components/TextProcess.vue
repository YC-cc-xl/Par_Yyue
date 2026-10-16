<template>
  <div class="text-page">
    <!-- ================= 顶部区域：打开文本 / 保存 ================= -->
    <div class="page-header">
      <span class="title">文本处理</span>
      <span class="subtitle">本地 txt 清洗 · 格式化 · 剪切 · 备份</span>
      <div class="header-actions">
        <el-button type="primary" @click="doOpen">打开文本</el-button>
        <el-button @click="doSave">保存</el-button>
        <span class="stat-divider">|</span>
        <span class="value-text">{{ filePath || '尚未打开文件' }}</span>
      </div>
    </div>

    <div class="page-body">
      <!-- ================= 主体：左【搜索结果】 + 中【编辑器】 + 右【常用处理】 ================= -->
      <div class="main-row">
        <!-- ---------- 左：搜索结果 ---------- -->
        <el-card shadow="never" class="panel col-left">
          <template #header>
            <div class="col-head">
              <span class="panel-title">搜索结果</span>
              <el-tag type="info" effect="plain" size="small">{{ matchTotal }} 处</el-tag>
            </div>
          </template>
          <SearchResultPanel
            :rows="searchRows"
            :total="matchTotal"
            :active-no="activeNo"
            @select="handleSelectMatch"
          />
        </el-card>

        <!-- ---------- 中：文本内容编辑器 ---------- -->
        <el-card shadow="never" class="panel col-center">
          <template #header>
            <div class="col-head">
              <span class="panel-title">文本内容</span>
              <div class="row-inline">
                <el-button size="small" @click="doSave">保存</el-button>
                <el-button size="small" @click="doSaveAs">另存为</el-button>
                <el-button size="small" :disabled="!filePath" @click="doReload">重新加载</el-button>
                <el-button size="small" type="primary" plain @click="openFindDialog">查找替换</el-button>
              </div>
            </div>
          </template>

          <!-- 原生 textarea：13 万字量级下比富文本编辑器更轻，滚动与选区都原生支持 -->
          <textarea
            ref="editorRef"
            v-model="content"
            class="editor"
            spellcheck="false"
            placeholder="在此查看或编辑大段文稿；也可点击「打开文本」加载本地 txt 文件"
            @input="handleInput"
            @click="updateCaret"
            @mouseup="updateCaret"
            @keyup="updateCaret"
          ></textarea>

          <!-- 底部状态栏：光标行列 / 字数 / 保存状态 / 编码 -->
          <div class="editor-status">
            <span class="status-item">第 {{ caret.line }} 行，第 {{ caret.col }} 列</span>
            <span class="status-divider">|</span>
            <span class="status-item">字数：{{ wordCount }}</span>
            <span class="status-divider">|</span>
            <span class="status-item" :class="{ 'is-dirty': dirty }">{{ dirty ? '未保存' : '已保存' }}</span>
            <template v-if="fileEncoding">
              <span class="status-divider">|</span>
              <span class="status-item">编码：{{ fileEncoding }}</span>
            </template>
          </div>
        </el-card>

        <!-- ---------- 右：常用处理 ---------- -->
        <el-card shadow="never" class="panel col-right">
          <template #header><span class="panel-title">常用处理</span></template>

          <div class="row-inline">
            <el-button size="small" :loading="jobRunning" @click="handleClean">智能清理广告</el-button>
            <el-button size="small" :loading="jobRunning" @click="handleFormat">智能格式化文本</el-button>
          </div>

          <div class="row-inline mt8">
            <span class="field-label">剪切字数</span>
            <el-input-number
              v-model="cutSize"
              :min="1"
              :max="5000000"
              :step="1000"
              controls-position="right"
              style="width: 150px"
            />
          </div>

          <div class="row-inline mt8">
            <el-button size="small" type="primary" plain @click="handleCutCopy">剪切并复制</el-button>
            <el-button size="small" :disabled="undoSnapshot === null" @click="handleUndoCut">撤销剪切</el-button>
          </div>

          <div class="row-inline mt8">
            <el-checkbox v-model="dropChapter">格式化时删除章节名称</el-checkbox>
          </div>

          <div class="hint-text">
            全部处理在本地离线执行（不联网、不调用外部 API）。长文本自动转后台任务，处理期间界面不冻结。
          </div>
        </el-card>
      </div>

      <!-- ================= 处理日志（分级过滤） ================= -->
      <el-card shadow="never" class="panel">
        <template #header>
          <div class="col-head">
            <span class="panel-title">处理日志</span>
            <div class="row-inline">
              <span v-if="jobRunning" class="value-text">{{ jobMessage }}</span>
              <el-select v-model="logLevel" size="small" style="width: 110px">
                <el-option v-for="lv in LOG_LEVELS" :key="lv" :label="lv" :value="lv" />
              </el-select>
            </div>
          </div>
        </template>
        <pre class="log-box">{{ filteredLog }}</pre>
      </el-card>
    </div>

    <!-- ================= 页面底部操作栏 ================= -->
    <div class="page-footer">
      <span class="footer-tip">{{ footerTip }}</span>
      <div class="row-inline">
        <el-button size="small" @click="doSaveAs">另存为</el-button>
        <el-button size="small" :loading="backupLoading" @click="handleBackup">备份</el-button>
        <el-button size="small" @click="openBackupDialog">恢复备份</el-button>
      </div>
    </div>

    <!-- ================= 查找替换模态弹窗 ================= -->
    <FindReplaceDialog
      v-model:visible="findVisible"
      v-model:find-text="findText"
      v-model:replace-text="replaceText"
      :match-total="matchTotal"
      :current-index="activeIndex"
      @find-next="handleFindNext"
      @replace-current="handleReplaceCurrent"
      @replace-all="handleReplaceAll"
    />

    <!-- ================= 恢复备份弹窗 ================= -->
    <el-dialog v-model="backupDialog" title="恢复备份" width="580px" append-to-body>
      <el-table v-if="backups.length" :data="backups" size="small" border stripe height="320">
        <el-table-column prop="file" label="备份文件" min-width="220" show-overflow-tooltip />
        <el-table-column prop="sizeText" label="大小" width="90" align="center" />
        <el-table-column prop="timeText" label="时间" width="150" align="center" />
        <el-table-column label="操作" width="86" align="center">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click="handleRestore(row)">恢复</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无备份" :image-size="60" />
      <div class="hint-text">
        恢复会用备份内容覆盖编辑器当前文本；备份文件保存在后端 data 目录，支持保留多份。
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
// ===========================================================================
// 文本处理页面：本地 txt 查看/编辑 + 前端查找替换 + 后端离线清洗/格式化/剪切 + 备份恢复
// 说明：查找替换全部在前端本地执行（毫秒级，不联网）；清洗/格式化/剪切调用本地后端。
// ===========================================================================
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import SearchResultPanel from './SearchResultPanel.vue'
import FindReplaceDialog from './FindReplaceDialog.vue'
import {
  buildLineIndex,
  findAll,
  lineIndexAt,
  lineTextAt,
  nextMatchIndex,
  normalizeNewlines,
  offsetToLineCol,
  replaceAllByOffsets,
  replaceOneByOffset
} from '@/utils/textSearch'

// ------------------------------ 常量 ------------------------------
// 日志级别（与项目既有日志体系一致）：DEBUG < INFO < WARN < ERROR
const LOG_LEVELS = ['DEBUG', 'INFO', 'WARN', 'ERROR']
const LOG_ORDER = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 }
// 超过该字数时，清洗/格式化转后端后台任务（避免前端长时间等待、界面卡住）
const ASYNC_THRESHOLD = 30000
// 后台任务轮询间隔与最大轮询次数（约 500ms × 2400 ≈ 20 分钟）
const POLL_INTERVAL = 500
const POLL_MAX = 2400
// 左侧搜索结果最多渲染条数（避免一次性创建上万行 DOM 造成卡顿）
const MAX_RENDER_MATCH = 5000

// ------------------------------ 编辑器状态 ------------------------------
const content = ref('') // 编辑器全文
const filePath = ref('') // 当前文件绝对路径（空 = 尚未打开文件）
const fileEncoding = ref('') // 最近一次读取文件所用的编码，仅用于展示
const dirty = ref(false) // 是否有未保存修改
const wordCount = ref(0) // 字数（输入时 debounce 统计，程序性变更立即统计）
const caret = ref({ line: 1, col: 1 }) // 光标位置（1 基行列）
const editorRef = ref(null) // textarea 元素引用

// 行索引（懒建）：content 变化时置脏，只在需要行列/查找时重建（13 万字一次约 2~5ms）
let lineStarts = [0]
let lineIndexDirty = true

// ------------------------------ 查找替换状态 ------------------------------
const findVisible = ref(false) // 弹窗显隐
const findText = ref('') // 查找内容
const replaceText = ref('') // 替换内容（留空 = 删除匹配文字）
const matches = ref([]) // 全部匹配的字符偏移（从小到大）
const activeIndex = ref(-1) // 当前匹配在 matches 中的下标

// ------------------------------ 常用处理状态 ------------------------------
const cutSize = ref(130000) // 剪切字数
const undoSnapshot = ref(null) // 剪切前的全文快照（单层，仅支持撤销一次）
const dropChapter = ref(false) // 格式化时是否删除章节名称
const jobRunning = ref(false) // 是否有后台处理任务在跑
const jobMessage = ref('') // 后台任务进度文案

// ------------------------------ 备份状态 ------------------------------
const backupDialog = ref(false)
const backupLoading = ref(false)
const backups = ref([])

// ------------------------------ 日志状态 ------------------------------
const logLevel = ref('INFO')
const logs = ref([])
let logSeq = 0

// ------------------------------ 定时器 ------------------------------
let wordTimer = null

// ============================== 派生数据 ==============================

// 字数统计文案 + 底部提示
const footerTip = computed(() => {
  const parts = [`共 ${wordCount.value} 字`]
  parts.push(filePath.value ? `来源：${filePath.value}` : '尚未打开文件')
  if (dirty.value) parts.push('有未保存修改')
  return `提示：${parts.join(' · ')}`
})

// 匹配总数
const matchTotal = computed(() => matches.value.length)
// 当前匹配序号（1 基，0 表示未定位）
const activeNo = computed(() => (activeIndex.value >= 0 ? activeIndex.value + 1 : 0))

// 左侧搜索结果行：序号 / 行号 / 所在内容（只渲染前 MAX_RENDER_MATCH 条）
const searchRows = computed(() => {
  if (!matches.value.length) return []
  ensureLineIndex()
  const text = content.value
  const limit = Math.min(matches.value.length, MAX_RENDER_MATCH)
  const rows = []
  for (let i = 0; i < limit; i++) {
    const offset = matches.value[i]
    const lineNo = lineIndexAt(lineStarts, offset)
    rows.push({ no: i + 1, line: lineNo + 1, text: lineTextAt(text, lineStarts, lineNo) })
  }
  return rows
})

// 日志按级别过滤后展示（与自动合成页的日志过滤方式一致）
const filteredLog = computed(() =>
  logs.value
    .filter((item) => LOG_ORDER[item.level] >= LOG_ORDER[logLevel.value])
    .map((item) => `[${item.level}] ${item.time} ${item.text}`)
    .join('\n')
)

// ============================== 基础工具 ==============================

/** 写入一条前端日志（分级，最多保留 300 条） */
function addLog(text, level = 'INFO') {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const time = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
  logs.value.push({ id: ++logSeq, time, level, text })
  if (logs.value.length > 300) logs.value.splice(0, logs.value.length - 300)
}

/** 行索引懒建：只有内容被改动过才重建 */
function ensureLineIndex() {
  if (lineIndexDirty) {
    lineStarts = buildLineIndex(content.value)
    lineIndexDirty = false
  }
}

/** 内容被修改后统一处理：标脏、标记未保存、安排字数统计 */
function markContentChanged() {
  lineIndexDirty = true
  dirty.value = true
  scheduleWordCount()
}

/** 字数统计：输入时 debounce 200ms，避免每次按键都触发统计与重渲染 */
function scheduleWordCount() {
  if (wordTimer) clearTimeout(wordTimer)
  wordTimer = setTimeout(() => {
    wordCount.value = content.value.length
  }, 200)
}

/** 程序性变更（打开/替换/恢复）后立即刷新字数（O(1)，无需 debounce） */
function refreshWordCount() {
  wordCount.value = content.value.length
}

/** 有新文本整体替换时，清空旧的查找结果（旧偏移已失效） */
function resetMatches() {
  matches.value = []
  activeIndex.value = -1
}

/** 更新光标行列（状态栏「第 X 行，第 X 列」） */
function updateCaret() {
  const ta = editorRef.value
  if (!ta) return
  ensureLineIndex()
  caret.value = offsetToLineCol(lineStarts, ta.selectionStart)
}

/** 把编辑器滚动到指定行（0 基行号），并尽量让该行居中 */
function scrollToLine(lineNo0) {
  const ta = editorRef.value
  if (!ta) return
  const lineHeight = parseFloat(window.getComputedStyle(ta).lineHeight) || 22
  const target = lineNo0 * lineHeight - ta.clientHeight / 2 + lineHeight / 2
  ta.scrollTop = Math.max(0, target)
}

/** 跳转到第 index 个匹配：设置选区（= 高亮）+ 滚动居中 + 同步光标行列 */
function jumpToMatch(index) {
  const list = matches.value
  if (index < 0 || index >= list.length) return
  const offset = list[index]
  const needleLen = findText.value.length
  activeIndex.value = index
  ensureLineIndex()
  const lineNo = lineIndexAt(lineStarts, offset)
  nextTick(() => {
    const ta = editorRef.value
    if (!ta) return
    try {
      ta.focus()
    } catch {
      // 弹窗是模态时可能被焦点陷阱拦下，不影响选区设置
    }
    ta.setSelectionRange(offset, offset + needleLen)
    scrollToLine(lineNo)
    caret.value = offsetToLineCol(lineStarts, offset)
  })
}

/** 统一错误提示 + 日志（接口异常不崩溃） */
function handleApiError(error, title) {
  const msg = (error && error.message) || String(error)
  addLog(`${title}：${msg}`, 'ERROR')
  ElMessage.error(`${title}：${msg}`)
}

// ============================== 编辑器输入 ==============================

function handleInput() {
  // v-model 已更新 content；这里只做标脏、字数统计与光标刷新
  markContentChanged()
  // 文本已改动，旧匹配偏移不可信：清空结果，等待下一次查找动作刷新
  if (matches.value.length) resetMatches()
  updateCaret()
}

// ============================== 查找替换逻辑（纯前端本地） ==============================

/** 打开查找替换弹窗：不清空已有高亮与结果列表（规范：只有新的查找动作才刷新） */
function openFindDialog() {
  findVisible.value = true
}

/** 重建全部匹配（每次新的查找动作都会调用） */
function refreshMatches(needle) {
  ensureLineIndex()
  matches.value = findAll(content.value, needle)
  activeIndex.value = -1
  return matches.value.length
}

/** 查找下一个：定位下一处匹配，到文末自动回到开头（循环查找） */
function handleFindNext() {
  const needle = findText.value
  if (!needle) {
    ElMessage.warning('请输入要查找的文字')
    return
  }
  const total = refreshMatches(needle)
  if (!total) {
    addLog(`未找到匹配：${needle}`, 'WARN')
    ElMessage.warning('未找到匹配内容')
    return
  }
  const ta = editorRef.value
  const fromOffset = ta ? ta.selectionStart : 0
  jumpToMatch(nextMatchIndex(matches.value, fromOffset))
  addLog(`查找「${needle}」：共 ${total} 处`, 'INFO')
}

/** 替换当前：只替换当前高亮选中的这一处 */
function handleReplaceCurrent() {
  const needle = findText.value
  if (!needle) {
    ElMessage.warning('请输入要查找的文字')
    return
  }
  if (activeIndex.value < 0 || activeIndex.value >= matches.value.length) {
    ElMessage.warning('请先点击「查找下一个」定位到匹配处')
    return
  }
  const offset = matches.value[activeIndex.value]
  const replacement = replaceText.value
  content.value = replaceOneByOffset(content.value, offset, needle, replacement)
  markContentChanged()
  refreshWordCount()
  // 替换后刷新结果列表，弹窗保持打开；把当前定位顺延到该位置之后的第一个匹配
  const total = refreshMatches(needle)
  if (total) {
    const idx = matches.value.findIndex((m) => m >= offset)
    jumpToMatch(idx === -1 ? 0 : idx)
  }
  addLog(`替换当前「${needle}」→「${replacement}」：1 处`, 'INFO')
}

/** 全部替换：一次性替换全文所有匹配；替换后刷新列表，弹窗不自动关闭 */
function handleReplaceAll() {
  const needle = findText.value
  if (!needle) {
    ElMessage.warning('请输入要查找的文字')
    return
  }
  if (!matches.value.length) refreshMatches(needle)
  if (!matches.value.length) {
    addLog(`未找到匹配：${needle}`, 'WARN')
    ElMessage.warning('未找到匹配内容')
    return
  }
  const count = matches.value.length
  const replacement = replaceText.value
  content.value = replaceAllByOffsets(content.value, matches.value, needle, replacement)
  markContentChanged()
  refreshWordCount()
  // 刷新结果列表（替换文本里若仍含查找词，会重新匹配到）
  const total = refreshMatches(needle)
  if (total) jumpToMatch(0)
  addLog(`全部替换「${needle}」→「${replacement}」：${count} 处`, 'INFO')
  ElMessage.success(`已替换 ${count} 处`)
}

/** 点击左侧搜索结果条目：编辑器跳转并高亮该匹配 */
function handleSelectMatch(no) {
  jumpToMatch(Number(no) - 1)
}

// 弹窗关闭后聚焦编辑器：让保留的选区高亮状态显示为激活态
watch(findVisible, (open) => {
  if (!open) {
    nextTick(() => {
      const ta = editorRef.value
      if (ta) ta.focus()
    })
  }
})

// ============================== 文件操作（走 Electron 主进程 IPC） ==============================

/** 有未保存修改时先确认，避免误丢内容 */
async function confirmDiscard(actionLabel) {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('当前文本有未保存的修改，继续将丢弃这些修改。是否继续？', actionLabel, {
      type: 'warning',
      confirmButtonText: '继续',
      cancelButtonText: '取消'
    })
    return true
  } catch {
    return false
  }
}

/** 打开的文本统一落到编辑器：换行归一化 + 重置状态 */
function applyLoadedText(path, text, encoding) {
  content.value = normalizeNewlines(text)
  filePath.value = path
  fileEncoding.value = encoding || ''
  dirty.value = false
  lineIndexDirty = true
  resetMatches()
  refreshWordCount()
  caret.value = { line: 1, col: 1 }
  addLog(`已打开：${path}（编码 ${encoding || '未知'}，${content.value.length} 字）`, 'INFO')
}

/** 读取文件（path 为空时弹系统对话框）；编码异常/读取失败给出友好提示且不崩溃 */
async function loadFile(path) {
  const bridge = window.electronAPI
  if (!bridge || !bridge.readTextFile) {
    ElMessage.error('文件读写不可用：请通过桌面客户端运行本程序')
    return
  }
  let res = null
  try {
    res = await bridge.readTextFile(path ? { path } : {})
  } catch (error) {
    handleApiError(error, '打开文件失败')
    return
  }
  if (!res) return // 用户取消
  if (res.error === 'unsupported-encoding') {
    addLog(`打开失败：无法识别编码 ${res.path}`, 'ERROR')
    ElMessageBox.alert(
      `无法识别文件编码（已尝试 UTF-8 / GBK / GB18030）：\n${res.path}\n\n建议：用记事本打开该文件，另存为 UTF-8 编码后再试。`,
      '编码不支持',
      { type: 'error', confirmButtonText: '知道了' }
    )
    return
  }
  if (res.error) {
    addLog(`打开失败：${res.message || res.error}`, 'ERROR')
    ElMessageBox.alert(`打开文件失败：\n${res.message || res.error}`, '打开失败', {
      type: 'error',
      confirmButtonText: '知道了'
    })
    return
  }
  applyLoadedText(res.path, res.content || '', res.encoding)
}

/** 打开文本 */
async function doOpen() {
  if (!(await confirmDiscard('打开文本'))) return
  await loadFile('')
}

/** 重新加载：用已打开文件的磁盘内容覆盖编辑器 */
async function doReload() {
  if (!filePath.value) {
    ElMessage.warning('尚未打开文件，无法重新加载')
    return
  }
  if (!(await confirmDiscard('重新加载'))) return
  await loadFile(filePath.value)
}

/** 保存 / 另存为：统一写 UTF-8（无 BOM） */
async function writeTo(path, askPath) {
  const bridge = window.electronAPI
  if (!bridge || !bridge.writeTextFile) {
    ElMessage.error('文件读写不可用：请通过桌面客户端运行本程序')
    return
  }
  let res = null
  try {
    res = await bridge.writeTextFile({ path, content: content.value, askPath })
  } catch (error) {
    handleApiError(error, '保存失败')
    return
  }
  if (!res) return // 用户取消
  if (res.error) {
    addLog(`保存失败：${res.message || res.error}`, 'ERROR')
    ElMessageBox.alert(`保存文件失败：\n${res.message || res.error}`, '保存失败', {
      type: 'error',
      confirmButtonText: '知道了'
    })
    return
  }
  filePath.value = res.path
  dirty.value = false
  addLog(`已保存：${res.path}（${content.value.length} 字）`, 'INFO')
  ElMessage.success('已保存')
}

async function doSave() {
  if (!filePath.value) {
    await doSaveAs()
    return
  }
  await writeTo(filePath.value, false)
}

async function doSaveAs() {
  await writeTo(filePath.value, true)
}

// ============================== 常用处理（后端离线执行） ==============================

/** 轮询后台任务直到结束；返回任务结果 */
async function pollJob(jobId) {
  for (let i = 0; i < POLL_MAX; i++) {
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL))
    const job = await api.getTextJob(jobId)
    if (job && job.message) jobMessage.value = `${job.message}${job.progress ? ` ${job.progress}%` : ''}`
    if (job && job.status === 'success') return job.result
    if (job && job.status === 'failed') throw new Error(job.error || '后台处理失败')
  }
  throw new Error('后台处理超时未完成')
}

/** 处理结果回填编辑器 */
function applyProcessedText(text, stats, kind) {
  const before = content.value.length
  content.value = normalizeNewlines(text || '')
  markContentChanged()
  refreshWordCount()
  resetMatches()
  const label = kind === 'clean' ? '智能清理广告' : '智能格式化文本'
  const detail =
    kind === 'clean'
      ? `删除广告/垃圾行 ${stats.removedLines || 0}、装饰分隔符行 ${stats.removedDecor || 0}、HTML 标签 ${stats.removedTags || 0}、转义字符 ${stats.removedEntities || 0}、合并空行 ${stats.collapsedBlank || 0}、清理空白 ${stats.trimmedSpaces || 0}`
      : `删除章节行 ${stats.removedChapter || 0}、合并断行 ${stats.mergedLines || 0}、修正标点 ${stats.fixedPunct || 0}、清理行尾标点 ${stats.trimmedTail || 0}`
  addLog(`${label}完成：${before} → ${content.value.length} 字；${detail}`, 'INFO')
  ElMessage.success(`${label}完成：${before} → ${content.value.length} 字`)
}

/** 执行清洗/格式化：长文本自动走后台任务，期间 UI 不冻结 */
async function runProcess(kind) {
  if (!content.value) {
    ElMessage.warning('编辑器内容为空，无法处理')
    return
  }
  if (jobRunning.value) {
    ElMessage.warning('已有处理任务进行中，请稍候')
    return
  }
  jobRunning.value = true
  jobMessage.value = kind === 'clean' ? '正在智能清理广告…' : '正在智能格式化文本…'
  const needAsync = content.value.length > ASYNC_THRESHOLD
  addLog(`${jobMessage.value}（${content.value.length} 字${needAsync ? '，已转后台任务' : ''}）`, 'INFO')
  try {
    const res =
      kind === 'clean'
        ? await api.cleanText(content.value, needAsync)
        : await api.formatText(content.value, dropChapter.value, needAsync)
    let payload = res
    if (res && res.async && res.jobId) payload = await pollJob(res.jobId)
    if (!payload || typeof payload.text !== 'string') throw new Error('后端返回结果异常')
    applyProcessedText(payload.text, payload.stats || {}, kind)
  } catch (error) {
    handleApiError(error, '文本处理失败')
  } finally {
    jobRunning.value = false
    jobMessage.value = ''
  }
}

function handleClean() {
  return runProcess('clean')
}

function handleFormat() {
  return runProcess('format')
}

// ============================== 剪切分割 ==============================

/** 写入系统剪贴板：优先走 Electron 主进程（file:// 下浏览器剪贴板不可用） */
async function copyToClipboard(text) {
  const bridge = window.electronAPI
  if (bridge && bridge.copyText) {
    let res = null
    try {
      res = await bridge.copyText(text)
    } catch (error) {
      addLog(`写入剪贴板异常：${error && error.message}`, 'ERROR')
    }
    if (res && res.ok) return true
    ElMessage.warning(`写入剪贴板失败（${(res && res.reason) || '未知原因'}），请手动复制`)
    return false
  }
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    ElMessage.warning('当前环境剪贴板不可用，请手动复制')
    return false
  }
}

/** 剪切并复制：取出第一段进剪贴板，剩余文本留在编辑器 */
async function handleCutCopy() {
  if (!content.value) {
    ElMessage.warning('编辑器内容为空，无法剪切')
    return
  }
  const size = Number(cutSize.value) || 0
  if (size <= 0) {
    ElMessage.warning('剪切字数必须大于 0')
    return
  }
  if (size >= content.value.length) {
    ElMessage.warning(`当前文本共 ${content.value.length} 字，不足 ${size} 字，无法剪切`)
    return
  }
  try {
    const res = await api.splitText(content.value, size)
    const first = (res && res.first) || ''
    const rest = (res && res.rest) || ''
    // 先复制成功再剪切：复制失败时保持原文，避免内容白丢
    if (!(await copyToClipboard(first))) return
    undoSnapshot.value = content.value // 单层快照：仅支持撤销一次
    content.value = normalizeNewlines(rest)
    markContentChanged()
    refreshWordCount()
    resetMatches()
    addLog(`剪切并复制：首段 ${first.length} 字已复制到剪贴板，编辑器剩余 ${content.value.length} 字`, 'INFO')
    ElMessage.success(`已复制首段 ${first.length} 字，编辑器剩余 ${content.value.length} 字`)
  } catch (error) {
    handleApiError(error, '剪切失败')
  }
}

/** 撤销剪切：恢复上一次剪切前的完整原文（仅一次） */
function handleUndoCut() {
  if (undoSnapshot.value === null) {
    ElMessage.warning('没有可撤销的剪切记录（仅支持撤销一次）')
    return
  }
  content.value = undoSnapshot.value
  undoSnapshot.value = null // 立即释放快照，同时保证只能撤销一次
  markContentChanged()
  refreshWordCount()
  resetMatches()
  addLog('已撤销上一次剪切，恢复剪切前全文', 'INFO')
  ElMessage.success('已撤销剪切')
}

// ============================== 备份 / 恢复 ==============================

async function loadBackups() {
  try {
    const res = await api.listBackups()
    const rows = (res && res.backups) || []
    backups.value = rows.map((row) => ({
      ...row,
      sizeText: `${Math.max(0.1, (row.size || 0) / 1024).toFixed(1)} KB`,
      timeText: row.time || ''
    }))
  } catch (error) {
    handleApiError(error, '读取备份列表失败')
  }
}

/** 备份当前编辑器文本（由后端写入本地备份文件，带时间戳） */
async function handleBackup() {
  if (!content.value) {
    ElMessage.warning('当前没有文本内容，无需备份')
    return
  }
  backupLoading.value = true
  try {
    const res = await api.createBackup(content.value, filePath.value ? `来源：${filePath.value}` : '')
    addLog(`备份完成：${res.file}（${res.size} 字节）`, 'INFO')
    ElMessage.success(`已备份：${res.file}`)
    await loadBackups()
  } catch (error) {
    handleApiError(error, '备份失败')
  } finally {
    backupLoading.value = false
  }
}

function openBackupDialog() {
  backupDialog.value = true
  loadBackups()
}

/** 恢复备份：覆盖编辑器内容（覆盖前确认） */
async function handleRestore(row) {
  try {
    await ElMessageBox.confirm(
      `确定用备份「${row.file}」覆盖编辑器当前内容吗？当前未保存的修改将丢失。`,
      '恢复备份',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    const res = await api.readBackup(row.file)
    content.value = normalizeNewlines((res && res.content) || '')
    markContentChanged()
    refreshWordCount()
    resetMatches()
    backupDialog.value = false
    addLog(`已恢复备份：${row.file}（${content.value.length} 字）`, 'INFO')
    ElMessage.success('已恢复到编辑器')
  } catch (error) {
    handleApiError(error, '恢复备份失败')
  }
}

// ============================== 生命周期 ==============================

onMounted(async () => {
  // 拉取后端历史操作日志（分级），与前端日志合并展示，便于排查
  try {
    const res = await api.listTextLogs('', 100)
    const rows = ((res && res.logs) || []).slice().reverse()
    rows.forEach((row) => {
      logs.value.push({
        id: ++logSeq,
        time: (row.created_at || '').slice(11) || '',
        level: row.level || 'INFO',
        text: `[后端] ${row.message}`
      })
    })
  } catch {
    // 后端未就绪时忽略（不影响页面使用）
  }
})

onBeforeUnmount(() => {
  if (wordTimer) clearTimeout(wordTimer)
})
</script>

<style scoped>
.text-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}

/* 顶部标题（与自动合成页保持一致的视觉语言） */
.page-header {
  flex: none;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 20px;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
}
.page-header .title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}
.page-header .subtitle {
  font-size: 12px;
  color: #909399;
}
.header-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

/* 内容区可滚动 */
.page-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px 8px;
}

.panel {
  margin-bottom: 14px;
  border-radius: 6px;
}
.panel :deep(.el-card__header) {
  padding: 10px 16px;
  background: #fafafa;
}
.panel :deep(.el-card__body) {
  padding: 14px 16px;
}
.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

/* 主体三栏：左搜索结果 / 中编辑器 / 右常用处理 */
.main-row {
  display: flex;
  align-items: stretch;
  gap: 14px;
}
.col-left {
  flex: none;
  width: 300px;
}
.col-center {
  flex: 1;
  min-width: 0;
}
.col-right {
  flex: none;
  width: 250px;
}
/* 面板头：标题与操作区左右分布 */
.col-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.row-inline {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.mt8 {
  margin-top: 8px;
}
.field-label {
  color: #606266;
  font-size: 13px;
}
.value-text {
  color: #909399;
  font-size: 13px;
  word-break: break-all;
}
.stat-divider {
  color: #dcdfe6;
}
.hint-text {
  margin-top: 10px;
  font-size: 12px;
  color: #a8abb2;
  line-height: 1.6;
}

/* 编辑器 */
.editor {
  width: 100%;
  height: 420px;
  box-sizing: border-box;
  padding: 10px 12px;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  font-size: 14px;
  line-height: 1.6;
  color: #303133;
  background: #fff;
  resize: vertical;
  outline: none;
  /* 全局样式给 body 加了 user-select:none，这里必须显式放开才能选中文字 */
  user-select: text;
  -webkit-user-select: text;
  font-family: "Microsoft YaHei", "PingFang SC", Consolas, monospace;
}
.editor:focus {
  border-color: #409eff;
}

/* 编辑器底部状态栏 */
.editor-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
}
.editor-status .status-divider {
  color: #dcdfe6;
}
.editor-status .is-dirty {
  color: #e6a23c;
}

/* 日志区（暗底等宽，与自动合成页一致） */
.log-box {
  margin: 0;
  height: 180px;
  overflow: auto;
  background: #1e1e1e;
  color: #d4d4d4;
  font-size: 12px;
  line-height: 1.6;
  padding: 10px 12px;
  border-radius: 4px;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: Consolas, "Courier New", monospace;
}

/* 底部操作栏 */
.page-footer {
  flex: none;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 20px;
  background: #fff;
  border-top: 1px solid #ebeef5;
}
.footer-tip {
  font-size: 13px;
  color: #909399;
  word-break: break-all;
}
</style>

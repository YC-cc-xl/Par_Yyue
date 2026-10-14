<template>
  <div class="compose-page">
    <!-- ================= 顶部标题 ================= -->
    <div class="page-header">
      <span class="title">自动合成</span>
      <span class="subtitle">批量组合：视频 + 配音 + 字幕 + BGM</span>
    </div>

    <div class="page-body">
      <!-- ================= 1. 参数预设 ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">参数预设</span></template>
        <div class="row-inline">
          <el-select
            v-model="presetId"
            placeholder="请选择预设"
            clearable
            style="width: 260px"
            :loading="presetLoading"
            @change="handlePresetChange"
          >
            <el-option v-for="p in presets" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
          <el-button type="primary" @click="savePresetDialog = true">保存预设</el-button>
          <el-button type="danger" plain :disabled="presetId == null" @click="handleDeletePreset">删除预设</el-button>
        </div>
      </el-card>

      <!-- ================= 2. 处理来源 ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">处理来源</span></template>
        <div class="row-inline">
          <el-input
            v-model="form.sourceDir"
            placeholder="请选择分集根目录（一个子文件夹 = 一集）"
            clearable
            style="flex: 1"
          />
          <el-button type="primary" plain @click="handleBrowseSource">浏览</el-button>
          <el-button type="success" :loading="scanning" @click="handleScan">扫描目录</el-button>
        </div>

        <!-- 扫描状态提示 -->
        <div class="scan-status" v-if="scanStatus">
          <el-alert :title="scanStatus" :type="scanStatusType" :closable="false" show-icon />
        </div>

        <!-- 分集清单 -->
        <div class="episode-list" v-if="episodes.length">
          <div class="list-head">
            <span>分集清单</span>
            <el-tag type="info" effect="plain">共 {{ episodes.length }} 集</el-tag>
          </div>
          <el-table :data="episodes" size="small" border stripe height="240">
            <el-table-column type="index" label="#" width="52" align="center" />
            <el-table-column prop="name" label="分集目录" min-width="180" show-overflow-tooltip />
            <el-table-column label="视频" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="row.videoPath ? 'success' : 'danger'" size="small" effect="plain">
                  {{ row.videoPath ? '已匹配' : '缺失' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="配音" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="row.audioPath ? 'success' : 'danger'" size="small" effect="plain">
                  {{ row.audioPath ? '已匹配' : '缺失' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="字幕" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="row.subtitlePath ? 'success' : 'warning'" size="small" effect="plain">
                  {{ row.subtitlePath ? '已匹配' : '无' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="匹配文件" min-width="220">
              <template #default="{ row }">
                <div class="file-cell">{{ row.videoPath || '-' }}</div>
                <div class="file-cell">{{ row.audioPath || '-' }}</div>
                <div class="file-cell">{{ row.subtitlePath || '-' }}</div>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <el-empty v-else description="尚未扫描，请先选择根目录后点击「扫描目录」" :image-size="70" />
      </el-card>

      <!-- ================= 3. 背景音乐 BGM ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">背景音乐 BGM</span></template>
        <div class="row-inline">
          <el-checkbox v-model="form.bgmEnabled">启用背景音乐</el-checkbox>
          <span class="field-label">模式</span>
          <el-select v-model="form.bgmMode" style="width: 160px" :disabled="!form.bgmEnabled">
            <el-option label="单文件循环" value="single_loop" />
          </el-select>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">BGM来源</span>
          <el-input v-model="form.bgmFile" placeholder="请选择 BGM 文件" :disabled="!form.bgmEnabled" style="flex: 1" />
          <el-button type="primary" plain :disabled="!form.bgmEnabled" @click="handleBrowseBgm">浏览</el-button>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">音量</span>
          <el-slider
            v-model="form.bgmVolume"
            :min="0"
            :max="100"
            :disabled="!form.bgmEnabled"
            style="flex: 1; max-width: 420px"
          />
          <span class="value-text">{{ form.bgmVolume }}%</span>
        </div>
      </el-card>

      <!-- ================= 4. 字幕 ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">字幕</span></template>
        <div class="row-inline">
          <el-checkbox v-model="form.subtitleEnabled">添加字幕</el-checkbox>
          <el-button type="primary" plain :disabled="!form.subtitleEnabled" @click="openSubtitleDialog">
            设置样式
          </el-button>
          <span class="value-text">
            {{ form.subtitleStyle.fontFamily }} / {{ form.subtitleStyle.fontSize }}px /
            {{ form.subtitleStyle.color }} / 描边{{ form.subtitleStyle.outlineWidth }}px /
            {{ positionLabel }}
          </span>
        </div>
        <div class="hint-text">不勾选「添加字幕」时不烧制字幕，仅做音视频合成。</div>
      </el-card>

      <!-- ================= 5. 输出选项 ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">输出选项</span></template>
        <div class="row-inline">
          <el-checkbox v-model="form.mergeVideo">创建合并视频</el-checkbox>
          <el-checkbox v-model="form.mergeWithFirst" :disabled="!form.mergeVideo">合并原视频与第一集</el-checkbox>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">自定义分辨率</span>
          <el-input-number v-model="form.width" :min="128" :max="7680" :step="2" controls-position="right" style="width: 130px" />
          <span class="x-sign">×</span>
          <el-input-number v-model="form.height" :min="128" :max="7680" :step="2" controls-position="right" style="width: 130px" />
          <span class="unit-text">px</span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">自定义码率</span>
          <el-input-number v-model="form.bitrate" :min="200" :max="50000" :step="100" controls-position="right" style="width: 160px" />
          <span class="unit-text">kbps</span>
        </div>
      </el-card>

      <!-- ================= 6. 性能限制 ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">性能限制</span></template>
        <div class="row-inline">
          <el-checkbox v-model="form.parallelEnabled">启用并行处理</el-checkbox>
          <span class="field-label ml16">最大任务数</span>
          <el-input-number
            v-model="form.maxWorkers"
            :min="1"
            :max="16"
            :disabled="!form.parallelEnabled"
            controls-position="right"
            style="width: 130px"
          />
        </div>
        <div class="row-inline mt8">
          <span class="field-label">性能上限</span>
          <el-slider v-model="form.perfLimit" :min="0" :max="100" style="flex: 1; max-width: 420px" />
          <span class="value-text">{{ form.perfLimit }}%</span>
        </div>
        <div class="hint-text">
          提示：并行处理可提升合成速度，但会显著增加 CPU / 内存占用；性能上限用于限制整体资源占用，请根据本机配置调整。
        </div>
      </el-card>

      <!-- ================= 任务进度 ================= -->
      <el-card shadow="never" class="panel" v-if="task">
        <template #header>
          <span class="panel-title">合成进度</span>
          <el-tag class="ml8" :type="taskTagType" effect="dark">{{ taskStatusLabel }}</el-tag>
        </template>
        <div class="row-inline">
          <span class="stat">总分集：<b>{{ task.total }}</b></span>
          <span class="stat success">成功：<b>{{ task.success }}</b></span>
          <span class="stat danger">失败：<b>{{ task.failed }}</b></span>
        </div>
        <el-progress class="mt8" :percentage="task.progress" :status="taskStatus" :stroke-width="14" />
        <div class="log-title">运行日志</div>
        <pre class="log-box">{{ task.log || '（暂无日志）' }}</pre>
      </el-card>
    </div>

    <!-- ================= 底部操作栏 ================= -->
    <div class="page-footer">
      <span class="footer-tip">{{ footerTip }}</span>
      <el-button type="primary" size="large" :loading="submitting" :disabled="scanning" @click="handleStartCompose">
        开始合成
      </el-button>
    </div>

    <!-- ================= 弹窗：保存预设 ================= -->
    <el-dialog v-model="savePresetDialog" title="保存参数预设" width="420px">
      <el-input v-model="presetNameInput" placeholder="请输入预设名称" maxlength="40" />
      <template #footer>
        <el-button @click="savePresetDialog = false">取消</el-button>
        <el-button type="primary" @click="handleSavePreset">保存</el-button>
      </template>
    </el-dialog>

    <!-- ================= 弹窗：字幕样式 ================= -->
    <el-dialog v-model="subtitleDialog" title="字幕样式设置" width="520px">
      <el-form label-width="86px">
        <el-form-item label="字体">
          <el-select v-model="styleDraft.fontFamily" style="width: 100%">
            <el-option v-for="f in fontOptions" :key="f" :label="f" :value="f" />
          </el-select>
        </el-form-item>
        <el-form-item label="字号">
          <el-input-number v-model="styleDraft.fontSize" :min="12" :max="120" controls-position="right" />
          <span class="unit-text ml8">px</span>
        </el-form-item>
        <el-form-item label="颜色">
          <el-color-picker v-model="styleDraft.color" />
          <span class="value-text ml8">{{ styleDraft.color }}</span>
        </el-form-item>
        <el-form-item label="描边颜色">
          <el-color-picker v-model="styleDraft.outlineColor" />
          <span class="value-text ml8">{{ styleDraft.outlineColor }}</span>
        </el-form-item>
        <el-form-item label="描边宽度">
          <el-input-number v-model="styleDraft.outlineWidth" :min="0" :max="10" controls-position="right" />
          <span class="unit-text ml8">px</span>
        </el-form-item>
        <el-form-item label="字幕位置">
          <el-select v-model="styleDraft.position" style="width: 100%">
            <el-option label="底部" value="bottom" />
            <el-option label="居中" value="center" />
            <el-option label="顶部" value="top" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="subtitleDialog = false">取消</el-button>
        <el-button type="primary" @click="handleConfirmSubtitleStyle">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive, ref, computed, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'

// ---------- 可选字体（Windows 常见字体） ----------
const fontOptions = ['微软雅黑', '黑体', '宋体', '楷体', '思源黑体', 'Arial']

// ---------- 页面表单：字段名与后端预设/任务结构保持一致 ----------
const form = reactive({
  // 处理来源
  sourceDir: '',
  // BGM
  bgmEnabled: false,
  bgmMode: 'single_loop',
  bgmFile: '',
  bgmVolume: 20,
  // 字幕
  subtitleEnabled: false,
  subtitleStyle: {
    fontFamily: '微软雅黑',
    fontSize: 28,
    color: '#FFFFFF',
    outlineColor: '#000000',
    outlineWidth: 2,
    position: 'bottom'
  },
  // 输出选项
  mergeVideo: true,
  mergeWithFirst: false,
  width: 1280,
  height: 720,
  bitrate: 2000,
  // 性能限制
  parallelEnabled: true,
  maxWorkers: 4,
  perfLimit: 80
})

// ---------- 参数预设 ----------
const presets = ref([])
const presetId = ref(null)
const presetLoading = ref(false)
const savePresetDialog = ref(false)
const presetNameInput = ref('')

// ---------- 扫描状态 ----------
const scanning = ref(false)
const scanStatus = ref('')
const scanStatusType = ref('info')
const episodes = ref([])

// ---------- 字幕样式弹窗 ----------
const subtitleDialog = ref(false)
// 草稿：确认后才写回 form.subtitleStyle，取消不影响原值
const styleDraft = reactive({ ...form.subtitleStyle })

// ---------- 任务与轮询 ----------
const submitting = ref(false)
const task = ref(null)
let pollTimer = null

// ---------- 派生展示 ----------
const positionLabel = computed(() => {
  const map = { bottom: '底部', center: '居中', top: '顶部' }
  return map[form.subtitleStyle.position] || '底部'
})

const footerTip = computed(() => {
  if (!form.sourceDir) return '请先选择处理来源根目录'
  if (!episodes.value.length) return '请先扫描目录，确认分集清单'
  return `共 ${episodes.value.length} 集待合成`
})

const taskStatusLabel = computed(() => {
  const map = { pending: '等待中', running: '合成中', completed: '已完成', failed: '失败' }
  return map[task.value?.status] || '未知'
})

const taskTagType = computed(() => {
  const map = { pending: 'info', running: 'warning', completed: 'success', failed: 'danger' }
  return map[task.value?.status] || 'info'
})

// 进度条状态：完成显示成功图标，失败显示错误图标，其余普通
const taskStatus = computed(() => {
  if (task.value?.status === 'completed') return 'success'
  if (task.value?.status === 'failed') return 'exception'
  return undefined
})

// ---------- 1. 参数预设 ----------
// 加载预设列表（阶段8 联调）
async function loadPresets() {
  presetLoading.value = true
  try {
    presets.value = (await api.listPresets()) || []
  } catch {
    // 后端未就绪时静默处理，不阻塞界面
    presets.value = []
  } finally {
    presetLoading.value = false
  }
}

// 选择预设后，将预设内容回填到表单
function handlePresetChange(id) {
  if (id == null) return
  const preset = presets.value.find((p) => p.id === id)
  if (!preset) return
  try {
    const cfg = typeof preset.config === 'string' ? JSON.parse(preset.config) : preset.config
    Object.keys(form).forEach((key) => {
      if (cfg[key] !== undefined) form[key] = cfg[key]
    })
    if (cfg.subtitleStyle) Object.assign(form.subtitleStyle, cfg.subtitleStyle)
    ElMessage.success(`已应用预设：${preset.name}`)
  } catch {
    ElMessage.error('预设内容解析失败')
  }
}

// 保存当前整页表单配置为新预设
async function handleSavePreset() {
  const name = presetNameInput.value.trim()
  if (!name) {
    ElMessage.warning('请输入预设名称')
    return
  }
  try {
    await api.savePreset(name, JSON.parse(JSON.stringify(form)))
    ElMessage.success('预设已保存')
    savePresetDialog.value = false
    presetNameInput.value = ''
    await loadPresets()
  } catch (e) {
    ElMessage.error(`保存失败：${e.message}`)
  }
}

// 删除当前选中预设
async function handleDeletePreset() {
  const preset = presets.value.find((p) => p.id === presetId.value)
  if (!preset) return
  try {
    await ElMessageBox.confirm(`确定删除预设「${preset.name}」？`, '提示', { type: 'warning' })
  } catch {
    return // 用户取消
  }
  try {
    await api.deletePreset(preset.id)
    ElMessage.success('已删除')
    presetId.value = null
    await loadPresets()
  } catch (e) {
    ElMessage.error(`删除失败：${e.message}`)
  }
}

// ---------- 2. 处理来源 ----------
// 后端就绪检查：Electron 环境下可读到主进程状态，未就绪时给出可操作提示
async function ensureBackendReady() {
  try {
    const status = await window.electronAPI?.getBackendStatus?.()
    if (status && !status.ready) {
      const tail = (status.tail || []).slice(-1)[0]
      ElMessage.error(`后端服务未就绪：${status.error || tail || '未知原因'}`)
      return false
    }
  } catch {
    // 非 Electron 环境（浏览器调试）跳过检查
  }
  return true
}

// 浏览选择根目录（调用 Electron 原生对话框）
async function handleBrowseSource() {
  const selected = await window.electronAPI?.selectFolder?.()
  if (selected) {
    form.sourceDir = selected
    // 目录变化后清空旧的分集清单
    episodes.value = []
    scanStatus.value = ''
  }
}

// 扫描目录：一个一级子文件夹 = 一个分集
async function handleScan() {
  if (!form.sourceDir) {
    ElMessage.warning('请先选择分集根目录')
    return
  }
  if (!(await ensureBackendReady())) return
  scanning.value = true
  scanStatus.value = '正在扫描目录…'
  scanStatusType.value = 'info'
  try {
    const res = await api.scanFolder(form.sourceDir)
    episodes.value = res?.episodes || []
    scanStatus.value = `扫描完成：共 ${res?.total ?? episodes.value.length} 集`
    scanStatusType.value = 'success'
  } catch (e) {
    episodes.value = []
    scanStatus.value = `扫描失败：${e.message}（需先启动后端服务）`
    scanStatusType.value = 'error'
  } finally {
    scanning.value = false
  }
}

// ---------- 3. BGM ----------
async function handleBrowseBgm() {
  const selected = await window.electronAPI?.selectBgmFile?.()
  if (selected) form.bgmFile = selected
}

// ---------- 4. 字幕样式 ----------
function openSubtitleDialog() {
  Object.assign(styleDraft, form.subtitleStyle)
  subtitleDialog.value = true
}

function handleConfirmSubtitleStyle() {
  Object.assign(form.subtitleStyle, styleDraft)
  subtitleDialog.value = false
}

// ---------- 5. 开始合成 ----------
function buildPayload() {
  return {
    sourceDir: form.sourceDir,
    bgm: {
      enabled: form.bgmEnabled,
      mode: form.bgmMode,
      file: form.bgmFile,
      volume: form.bgmVolume
    },
    subtitle: {
      enabled: form.subtitleEnabled,
      style: { ...form.subtitleStyle }
    },
    output: {
      mergeVideo: form.mergeVideo,
      mergeWithFirst: form.mergeWithFirst,
      width: form.width,
      height: form.height,
      bitrate: form.bitrate
    },
    performance: {
      parallelEnabled: form.parallelEnabled,
      maxWorkers: form.maxWorkers,
      perfLimit: form.perfLimit
    }
  }
}

async function handleStartCompose() {
  if (!form.sourceDir) {
    ElMessage.warning('请先选择处理来源根目录')
    return
  }
  if (form.bgmEnabled && !form.bgmFile) {
    ElMessage.warning('已启用背景音乐，请选择 BGM 文件')
    return
  }
  if (!(await ensureBackendReady())) return
  submitting.value = true
  try {
    const res = await api.createTask(buildPayload())
    task.value = {
      id: res?.id,
      total: res?.total ?? 0,
      success: 0,
      failed: 0,
      progress: 0,
      status: 'running',
      log: ''
    }
    ElMessage.success('合成任务已创建')
    startPolling()
  } catch (e) {
    ElMessage.error(`创建任务失败：${e.message}（需先启动后端服务）`)
  } finally {
    submitting.value = false
  }
}

// 轮询任务进度（阶段8 联调）
function startPolling() {
  stopPolling()
  pollTimer = setInterval(refreshTask, 1000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function refreshTask() {
  if (!task.value?.id) return
  try {
    const res = await api.getTask(task.value.id)
    if (!res) return
    task.value = { ...task.value, ...res }
    if (res.status === 'completed' || res.status === 'failed') stopPolling()
  } catch {
    // 轮询失败保持静默，等待下次重试
  }
}

// 组件卸载时清理定时器
onBeforeUnmount(stopPolling)

// 初始化：加载预设列表
loadPresets()
</script>

<style scoped>
.compose-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}

/* 顶部标题 */
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

/* 通用横排布局 */
.row-inline {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.mt8 {
  margin-top: 8px;
}
.ml8 {
  margin-left: 8px;
}
.ml16 {
  margin-left: 16px;
}
.field-label {
  color: #606266;
  font-size: 13px;
}
.value-text {
  color: #909399;
  font-size: 13px;
}
.unit-text {
  color: #909399;
  font-size: 13px;
}
.x-sign {
  color: #909399;
  padding: 0 2px;
}
.hint-text {
  margin-top: 10px;
  font-size: 12px;
  color: #a8abb2;
  line-height: 1.6;
}

/* 扫描状态 */
.scan-status {
  margin-top: 10px;
}

/* 分集清单 */
.episode-list {
  margin-top: 12px;
}
.list-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.file-cell {
  font-size: 12px;
  color: #909399;
  line-height: 1.7;
  word-break: break-all;
}

/* 统计文字 */
.stat {
  font-size: 13px;
  color: #606266;
  margin-right: 18px;
}
.stat.success b {
  color: #67c23a;
}
.stat.danger b {
  color: #f56c6c;
}

/* 日志区 */
.log-title {
  margin-top: 12px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.log-box {
  margin: 8px 0 0;
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
  padding: 0 20px;
  background: #fff;
  border-top: 1px solid #ebeef5;
}
.footer-tip {
  font-size: 13px;
  color: #909399;
}
</style>

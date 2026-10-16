// 前端接口层：统一封装对 FastAPI 后端的请求
// 端口来源：Electron 主进程通过 preload 暴露；非 Electron 环境（纯浏览器调试）回退 8123
// 说明：后端接口在【阶段4】实现，本文件仅定义调用方式，阶段8 完成联调

const DEFAULT_PORT = 8123
let cachedBaseUrl = null

// 解析后端基础地址（只解析一次并缓存）
async function resolveBaseUrl() {
  if (cachedBaseUrl) return cachedBaseUrl
  let port = DEFAULT_PORT
  try {
    const p = await window.electronAPI?.getBackendPort?.()
    if (p) port = p
  } catch {
    // 忽略：非 Electron 环境
  }
  cachedBaseUrl = `http://127.0.0.1:${port}`
  return cachedBaseUrl
}

// 通用请求：自动 JSON 序列化，非 2xx 抛出后端返回的错误信息
async function request(path, { method = 'GET', body } = {}) {
  const baseUrl = await resolveBaseUrl()
  const res = await fetch(`${baseUrl}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  const text = await res.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || `请求失败(${res.status})`
    throw new Error(msg)
  }
  return data
}

export const api = {
  // 扫描一级项目根目录（支持批量）：返回各项目/分集/素材组与续跑跳过信息
  scanFolders: (sourceDirs) =>
    request('/api/compose/scan', { method: 'POST', body: { sourceDirs } }),
  // 兼容旧调用：单个根目录
  scanFolder: (sourceDir) => request('/api/compose/scan', { method: 'POST', body: { sourceDir } }),

  // 参数预设
  listPresets: () => request('/api/compose/presets'),
  savePreset: (name, config) => request('/api/compose/presets', { method: 'POST', body: { name, config } }),
  deletePreset: (id) => request(`/api/compose/presets/${id}`, { method: 'DELETE' }),

  // 批量合成任务
  createTask: (payload) => request('/api/compose/tasks', { method: 'POST', body: payload }),
  getTask: (id) => request(`/api/compose/tasks/${id}`),
  // 任务进度：全局进度（已完成组/总组）+ 各分集/各项目独立进度与状态，供前端轮询刷新
  getTaskProgress: (id) => request(`/api/compose/tasks/${id}/progress`),
  // 手动取消任务（整单）：计时停止，终态 canceled，不触发完成弹窗
  cancelTask: (id) => request(`/api/compose/tasks/${id}/cancel`, { method: 'POST' }),

  // 项目级控制：单独暂停/恢复/取消某一个一级项目，不影响其他项目
  pauseProject: (id, projectIndex) =>
    request(`/api/compose/tasks/${id}/projects/${projectIndex}/pause`, { method: 'POST' }),
  resumeProject: (id, projectIndex) =>
    request(`/api/compose/tasks/${id}/projects/${projectIndex}/resume`, { method: 'POST' }),
  cancelProject: (id, projectIndex) =>
    request(`/api/compose/tasks/${id}/projects/${projectIndex}/cancel`, { method: 'POST' }),

  // 实时系统资源：CPU 占用 / 磁盘读写 / 是否饱和（供资源监控面板轮询）
  getSystemResources: () => request('/api/system/resources'),

  // ===================== 文本处理（本地离线执行，不联网）=====================

  // 智能清理广告：asyncJob 为真（或文本过长）时后端转后台任务，返回 { jobId, async: true }
  cleanText: (text, asyncJob = false) =>
    request('/api/text/clean', { method: 'POST', body: { text, asyncJob } }),
  // 智能格式化文本：dropChapter 为真时同时删除章节标题行
  formatText: (text, dropChapter = false, asyncJob = false) =>
    request('/api/text/format', { method: 'POST', body: { text, dropChapter, asyncJob } }),
  // 按字数剪切：返回 { first, rest }，第一段用于复制到剪贴板，剩余留在编辑器
  splitText: (text, size) => request('/api/text/split', { method: 'POST', body: { text, size } }),
  // 查询后台文本任务：{ status, progress, message, result }
  getTextJob: (jobId) => request(`/api/text/jobs/${encodeURIComponent(jobId)}`),
  // 文本处理操作日志（分级 DEBUG/INFO/WARN/ERROR）
  listTextLogs: (jobId = '', limit = 200) =>
    request(`/api/text/logs?${jobId ? `jobId=${encodeURIComponent(jobId)}&` : ''}limit=${limit}`),
  // 备份当前文本（本地文件，带时间戳，支持多份）
  createBackup: (text, note = '') => request('/api/text/backups', { method: 'POST', body: { text, note } }),
  // 历史备份列表
  listBackups: () => request('/api/text/backups'),
  // 读取指定备份内容，用于恢复
  readBackup: (name) => request(`/api/text/backups/${encodeURIComponent(name)}`)
}

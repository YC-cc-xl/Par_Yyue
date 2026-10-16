// 预加载脚本：以 contextBridge 安全地向渲染进程暴露能力
// 阶段2：文件夹/文件选择 + 后端端口
// 阶段7：追加后端运行状态查询
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  platform: process.platform,

  // 弹出系统对话框选择根目录，返回选中的绝对路径；取消返回 null
  selectFolder: () => ipcRenderer.invoke('dialog:select-folder'),

  // 弹出系统对话框选择单个 BGM 文件，返回绝对路径；取消返回 null
  selectBgmFile: () => ipcRenderer.invoke('dialog:select-bgm-file'),

  // 获取 FastAPI 后端端口（后端被占用时会自动换端口，故需从主进程取）
  getBackendPort: () => ipcRenderer.invoke('backend:get-port'),

  // 获取后端运行状态：{ ready, port, error, tail }
  getBackendStatus: () => ipcRenderer.invoke('backend:get-status'),

  // ===== 文本处理（本地文件与剪贴板，纯离线）=====

  // 打开/读取文本文件：
  //   不带 path 时弹系统打开对话框；带 path 时直接读取（供「重新加载」）
  //   返回 { path, content, encoding } | null | { error }
  readTextFile: (payload) => ipcRenderer.invoke('file:read-text', payload || {}),

  // 保存/另存为文本文件：askPath 为真时弹保存对话框，统一写 UTF-8 无 BOM
  //   返回 { path } | null | { error }
  writeTextFile: (payload) => ipcRenderer.invoke('file:write-text', payload || {}),

  // 写入系统剪贴板（file:// 下 navigator.clipboard 不可用，统一走主进程）
  //   返回 { ok, length } | { ok: false, reason }
  copyText: (text) => ipcRenderer.invoke('clipboard:write-text', { text })
})

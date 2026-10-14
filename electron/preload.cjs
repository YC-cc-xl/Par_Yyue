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
  getBackendStatus: () => ipcRenderer.invoke('backend:get-status')
})

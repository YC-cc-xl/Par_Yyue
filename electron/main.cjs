// Electron 主进程
// 阶段1：创建窗口
// 阶段2：新增文件夹/文件选择对话框 IPC（供页面「浏览」按钮调用）
// 阶段7：自动拉起 / 退出时杀死 Python(FastAPI) 后端进程
const { app, BrowserWindow, ipcMain, dialog, clipboard } = require('electron')
const path = require('node:path')
const fs = require('node:fs')
const http = require('node:http')
const net = require('node:net')
const { spawn, spawnSync } = require('node:child_process')

// scripts/dev.mjs 启动时会注入该环境变量，用于区分开发/生产
const VITE_DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL
const isDev = Boolean(VITE_DEV_SERVER_URL)
const PROJECT_ROOT = path.join(__dirname, '..')

// FastAPI 后端首选端口（被占用时自动向后探测空闲端口）
const BACKEND_PORT = 8123
const BACKEND_START_TIMEOUT = 30000

let mainWindow = null

// ============ 路径解析（开发 / 打包后两种目录结构）============

// 后端目录：打包后 backend 由 extraResources 复制到 resources/backend
function resolvePackedBackendDir() {
  if (!isDev && process.resourcesPath) {
    const packed = path.join(process.resourcesPath, 'backend')
    if (fs.existsSync(packed)) return packed
  }
  return null
}

// Python 解释器：优先专用虚拟环境，其次随包携带的解释器，最后回退到 PATH
function resolvePython(baseDir) {
  const venvPython = process.platform === 'win32'
    ? path.join('Scripts', 'python.exe')
    : path.join('bin', 'python3')
  const candidates = []
  if (process.env.COMPOSE_PYTHON) candidates.push(process.env.COMPOSE_PYTHON)
  candidates.push(path.join(baseDir, '.venv', venvPython))
  if (!isDev && process.resourcesPath) {
    candidates.push(path.join(process.resourcesPath, 'python',
      process.platform === 'win32' ? 'python.exe' : path.join('bin', 'python3')))
  }
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

// 后端启动目标：打包后优先运行 PyInstaller 冻结的 backend.exe（无需 Python 环境）；
// 开发模式运行 backend/main.py
function resolveBackendTarget() {
  const packedDir = resolvePackedBackendDir()
  if (packedDir) {
    const exe = path.join(packedDir, 'backend.exe')
    if (fs.existsSync(exe)) return { dir: packedDir, cmd: exe, args: [] }
    const script = path.join(packedDir, 'main.py')
    if (fs.existsSync(script)) return { dir: packedDir, cmd: resolvePython(packedDir), args: [script] }
  }
  const dir = path.join(PROJECT_ROOT, 'backend')
  return { dir, cmd: resolvePython(dir), args: [path.join(dir, 'main.py')] }
}

// ffmpeg：程序目录 ffmpeg/ 下的二进制（打包后位于 resources/ffmpeg）
function resolveFfmpeg() {
  const name = process.platform === 'win32' ? 'ffmpeg.exe' : 'ffmpeg'
  const candidates = [process.env.COMPOSE_FFMPEG, path.join(PROJECT_ROOT, 'ffmpeg', name)]
  if (process.resourcesPath) candidates.push(path.join(process.resourcesPath, 'ffmpeg', name))
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) return candidate
  }
  return null
}

// ============ 后端进程管理 ============

let backendChild = null      // 当前后端子进程（未启动/已退出时为 null）
let backendPid = null        // 用于退出时兜底清理（含 ffmpeg 子进程树）
let backendPort = BACKEND_PORT
let backendReady = false
let backendError = null
let isQuitting = false       // 主动退出标志：避免把正常关闭记成「意外退出」
let logTail = []             // 后端输出末尾若干行，便于界面/日志定位启动失败原因

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// 探测某端口是否空闲
function isPortFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(false))
    server.once('listening', () => server.close(() => resolve(true)))
    server.listen(port, '127.0.0.1')
  })
}

// 从首选端口向后找一个空闲端口（最多尝试 20 个）
async function findFreePort(startPort, tries = 20) {
  for (let i = 0; i < tries; i += 1) {
    const port = startPort + i
    if (await isPortFree(port)) return port
  }
  return startPort
}

// 单次健康检查
function checkHealth(port) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: '/api/health', timeout: 1500 },
      (res) => {
        res.resume()
        resolve(res.statusCode === 200)
      }
    )
    req.on('error', () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
  })
}

// 轮询等待后端就绪；后端进程提前退出则立即失败
async function waitForBackend(port, maxMs = BACKEND_START_TIMEOUT) {
  const deadline = Date.now() + maxMs
  while (Date.now() < deadline) {
    if (backendChild && backendChild.exitCode !== null) return false
    if (await checkHealth(port)) return true
    await sleep(400)
  }
  return false
}

// 把子进程输出按行转发到主进程控制台，并保留末尾若干行用于诊断
function forwardOutput(stream, tag) {
  stream.setEncoding('utf-8')
  stream.on('data', (chunk) => {
    for (const line of String(chunk).split(/\r?\n/)) {
      if (!line.trim()) continue
      console.log(`${tag} ${line}`)
      logTail.push(line)
      if (logTail.length > 30) logTail.shift()
    }
  })
}

function startBackend() {
  const target = resolveBackendTarget()
  const ffmpeg = resolveFfmpeg()

  const env = {
    ...process.env,
    COMPOSE_PORT: String(backendPort),   // 后端按此端口监听
    PYTHONIOENCODING: 'utf-8',           // 保证中文日志不乱码
    PYTHONUNBUFFERED: '1'
  }
  if (ffmpeg) env.COMPOSE_FFMPEG = ffmpeg
  else console.warn('[backend] 未找到 ffmpeg.exe，渲染阶段会报错')

  // 打包后 resources 目录通常不可写，数据库改放用户数据目录
  if (!isDev) {
    const dataDir = path.join(app.getPath('userData'), 'data')
    fs.mkdirSync(dataDir, { recursive: true })
    env.COMPOSE_DB_PATH = path.join(dataDir, 'compose.db')
  }

  const args = [...target.args, '--port', String(backendPort)]
  console.log(`[backend] 启动：${target.cmd} ${args.join(' ')}`)
  backendChild = spawn(target.cmd, args, {
    cwd: target.dir,
    env,
    windowsHide: true,
    // 非 Windows 需要独立进程组，退出时才能整组杀死
    detached: process.platform !== 'win32',
    stdio: ['ignore', 'pipe', 'pipe']
  })
  backendPid = backendChild.pid

  forwardOutput(backendChild.stdout, '[backend]')
  forwardOutput(backendChild.stderr, '[backend:err]')

  backendChild.on('error', (err) => {
    backendError = `后端进程启动失败：${err.message}`
    console.error('[backend]', backendError)
  })

  backendChild.on('exit', (code, signal) => {
    console.log(`[backend] 进程退出（code=${code} signal=${signal}）`)
    backendChild = null
    backendPid = null
    backendReady = false
    if (!isQuitting && !backendError) backendError = `后端进程意外退出（code=${code}）`
  })
}

// 结束后端进程及其子进程（ffmpeg）；用同步杀进程树保证退出时不被中断
function stopBackend() {
  isQuitting = true
  const pid = backendPid
  backendPid = null
  backendChild = null
  backendReady = false
  if (!pid) return
  try {
    if (process.platform === 'win32') {
      // 用绝对路径，避免 PATH 中缺少 System32 时找不到 taskkill
      const winDir = process.env.SystemRoot || process.env.windir || 'C:\\Windows'
      const taskkill = path.join(winDir, 'System32', 'taskkill.exe')
      spawnSync(fs.existsSync(taskkill) ? taskkill : 'taskkill',
        ['/pid', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
    } else {
      try { process.kill(-pid, 'SIGKILL') } catch { process.kill(pid, 'SIGKILL') }
    }
    console.log(`[backend] 已结束后端进程树（pid=${pid}）`)
  } catch (err) {
    console.error('[backend] 结束后端进程失败：', err.message)
  }
}

// 启动后端 → 等待就绪 → 创建窗口
async function boot() {
  backendPort = await findFreePort(BACKEND_PORT)
  startBackend()
  backendReady = await waitForBackend(backendPort)
  if (!backendReady && !backendError) {
    backendError = `后端启动超时（${BACKEND_START_TIMEOUT / 1000}s）`
    console.error('[backend]', backendError)
  }
  createWindow()
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 1024,
    minHeight: 700,
    title: '自动合成',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  if (VITE_DEV_SERVER_URL) {
    mainWindow.loadURL(VITE_DEV_SERVER_URL)
    // 默认不自动打开开发者工具（避免其联网加载前端资源产生报错），按 F12 可手动打开
    if (process.env.OPEN_DEVTOOLS === '1') {
      mainWindow.webContents.openDevTools({ mode: 'detach' })
    }
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

// ============ IPC：渲染进程 → 主进程 ============

// 选择「处理来源」根文件夹
ipcMain.handle('dialog:select-folder', async () => {
  const result = await dialog.showOpenDialog(mainWindow || undefined, {
    title: '选择分集根目录',
    properties: ['openDirectory', 'createDirectory']
  })
  if (result.canceled || result.filePaths.length === 0) return null
  return result.filePaths[0]
})

// 选择 BGM 背景音乐文件（单个文件）
ipcMain.handle('dialog:select-bgm-file', async () => {
  const result = await dialog.showOpenDialog(mainWindow || undefined, {
    title: '选择背景音乐文件',
    properties: ['openFile'],
    filters: [{ name: '音频文件', extensions: ['mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg'] }]
  })
  if (result.canceled || result.filePaths.length === 0) return null
  return result.filePaths[0]
})

// 提供后端端口给渲染进程（阶段8 请求接口时使用）
ipcMain.handle('backend:get-port', () => backendPort)

// 后端运行状态（阶段8 可用于提示「后端未就绪」）
ipcMain.handle('backend:get-status', () => ({
  ready: backendReady,
  port: backendPort,
  error: backendError,
  tail: logTail.slice(-10)
}))

// ============ IPC：文本处理（本地文件读写 / 剪贴板，纯离线）============

// 文本文件对话框过滤器：默认只看 txt/md/log，另给「所有文件」兜底
const TEXT_FILE_FILTERS = [
  { name: '文本文件', extensions: ['txt', 'md', 'log'] },
  { name: '所有文件', extensions: ['*'] }
]

/**
 * 解码文本字节流：自动兼容 utf-8 / gbk / gb18030
 * 流程：先剥掉 UTF-8 BOM（EF BB BF），再用 fatal 模式逐个编码尝试；
 *      全部失败返回 null（由调用方转成结构化错误，不让异常抛到渲染进程）
 */
function decodeTextBuffer(buffer) {
  let data = buffer
  if (data.length >= 3 && data[0] === 0xef && data[1] === 0xbb && data[2] === 0xbf) {
    data = data.subarray(3)
  }
  for (const encoding of ['utf-8', 'gbk', 'gb18030']) {
    try {
      return { content: new TextDecoder(encoding, { fatal: true }).decode(data), encoding }
    } catch {
      // 该编码解不出来，继续尝试下一种
    }
  }
  return null
}

// 打开 / 读取文本文件：
//   带 path 时直接读取（供「重新加载」用，不弹对话框）
//   不带 path 时弹系统打开对话框
// 返回 { path, content, encoding }；取消返回 null；失败返回 { error }
ipcMain.handle('file:read-text', async (_event, payload = {}) => {
  let target = payload && payload.path ? String(payload.path) : ''
  if (!target) {
    const result = await dialog.showOpenDialog(mainWindow || undefined, {
      title: '打开文本文件',
      properties: ['openFile'],
      filters: TEXT_FILE_FILTERS
    })
    if (result.canceled || result.filePaths.length === 0) return null
    target = result.filePaths[0]
  }
  try {
    const buffer = fs.readFileSync(target)
    const decoded = decodeTextBuffer(buffer)
    if (!decoded) return { path: target, error: 'unsupported-encoding' }
    return { path: target, content: decoded.content, encoding: decoded.encoding }
  } catch (e) {
    // 文件不存在/被占用/无权限等：返回结构化错误，前端弹友好提示
    return { path: target, error: 'read-failed', message: String((e && e.message) || e) }
  }
})

// 保存 / 另存为文本文件：
//   askPath 为真（或没有已知路径）时弹保存对话框；统一写为 UTF-8（无 BOM）
// 返回 { path }；取消返回 null；失败返回 { error }
ipcMain.handle('file:write-text', async (_event, payload = {}) => {
  let target = payload && payload.path ? String(payload.path) : ''
  const content = typeof (payload && payload.content) === 'string' ? payload.content : ''
  if (payload && payload.askPath) {
    const result = await dialog.showSaveDialog(mainWindow || undefined, {
      title: '保存文本文件',
      defaultPath: target || '新建文本.txt',
      filters: TEXT_FILE_FILTERS
    })
    if (result.canceled || !result.filePath) return null
    target = result.filePath
  }
  if (!target) return null
  try {
    fs.writeFileSync(target, content, 'utf8')
    return { path: target }
  } catch (e) {
    return { path: target, error: 'write-failed', message: String((e && e.message) || e) }
  }
})

// 写入系统剪贴板：
// 生产环境页面是 file://（非安全上下文），渲染进程 navigator.clipboard 不可用，
// 因此统一走主进程 clipboard 模块
ipcMain.handle('clipboard:write-text', (_event, payload = {}) => {
  try {
    const text = typeof (payload && payload.text) === 'string' ? payload.text : ''
    clipboard.writeText(text)
    return { ok: true, length: text.length }
  } catch (e) {
    return { ok: false, reason: String((e && e.message) || e) }
  }
})

app.whenReady().then(boot)

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow()
})

// 关闭主窗口 → 杀死后端进程 → 退出应用
app.on('window-all-closed', () => {
  stopBackend()
  if (process.platform !== 'darwin') app.quit()
})

// 兜底：无论以何种方式退出，都确保后端进程树被清理
app.on('before-quit', () => stopBackend())
app.on('will-quit', () => stopBackend())

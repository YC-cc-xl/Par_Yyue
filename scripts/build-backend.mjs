// 阶段8：把 Python 后端冻结为可独立运行的 backend.exe
// 产出：build/pyinstaller/backend/backend.exe（onedir，含全部依赖）
// 由 electron-builder 通过 extraResources 复制到 resources/backend，
// 最终用户机器无需安装 Python。
//
// 用法：node scripts/build-backend.mjs
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const backendDir = path.join(root, 'backend')
const outRoot = path.join(root, 'build', 'pyinstaller')
const workRoot = path.join(root, 'build', 'pyinstaller-work')

// 定位后端使用的 Python 解释器（与 electron/main.cjs 的解析顺序保持一致）
function findPython() {
  const venvPython = process.platform === 'win32'
    ? path.join('Scripts', 'python.exe')
    : path.join('bin', 'python3')
  const candidates = [process.env.COMPOSE_PYTHON, path.join(backendDir, '.venv', venvPython)]
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) return candidate
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

function run(cmd, args, opts = {}) {
  const result = spawnSync(cmd, args, { stdio: 'inherit', cwd: root, ...opts })
  return result.status === 0
}

const python = findPython()
console.log(`[build-backend] Python: ${python}`)

// 1. 确保 PyInstaller 可用（uv 创建的虚拟环境默认不带 pip，优先用 uv 安装）
function hasPyInstaller() {
  return spawnSync(python, ['-m', 'PyInstaller', '--version'], { stdio: 'ignore' }).status === 0
}

if (!hasPyInstaller()) {
  console.log('[build-backend] 未检测到 PyInstaller，开始安装（仅构建期依赖，不影响运行时）…')
  const uv = process.env.COMPOSE_UV || 'uv'
  const okUv = spawnSync(uv, ['pip', 'install', '--python', python, 'pyinstaller'], {
    stdio: 'inherit',
    cwd: root
  }).status === 0
  if (!okUv) {
    // 回退到 pip（若该解释器自带 pip）
    run(python, ['-m', 'pip', 'install', 'pyinstaller'])
  }
  if (!hasPyInstaller()) {
    console.error('[build-backend] PyInstaller 安装失败，请手动执行：uv pip install --python "<python>" pyinstaller')
    process.exit(1)
  }
}

// 2. 清理旧产物，执行冻结（onedir 模式：启动更快、更稳定）
fs.rmSync(outRoot, { recursive: true, force: true })
fs.rmSync(workRoot, { recursive: true, force: true })

const args = [
  '--noconfirm',
  '--clean',
  '--name', 'backend',
  '--distpath', outRoot,
  '--workpath', workRoot,
  '--specpath', path.join(root, 'build'),
  '--paths', backendDir,
  // uvicorn 通过字符串动态导入 loop / protocol 实现，必须显式收集子模块
  '--collect-submodules', 'uvicorn',
  '--collect-submodules', 'fastapi',
  '--collect-submodules', 'starlette',
  '--collect-submodules', 'pydantic',
  '--collect-submodules', 'anyio',
  path.join(backendDir, 'main.py')
]

console.log('[build-backend] 开始 PyInstaller 打包…')
if (!run(python, ['-m', 'PyInstaller', ...args])) {
  console.error('[build-backend] PyInstaller 打包失败')
  process.exit(1)
}

const exePath = path.join(outRoot, 'backend', 'backend.exe')
if (!fs.existsSync(exePath)) {
  console.error(`[build-backend] 未生成预期产物：${exePath}`)
  process.exit(1)
}
console.log(`[build-backend] 完成 → ${exePath}`)

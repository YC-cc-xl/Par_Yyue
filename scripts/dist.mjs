// 一键打包脚本（阶段8）
// 步骤：准备 rcedit → Vite 构建前端 → PyInstaller 冻结后端 → electron-builder 生成 NSIS 安装包
// 说明：electron-builder 内部的 Go 工具只读环境变量（不读项目 .npmrc），
//       因此这里统一注入国内镜像，避免从 GitHub 下载 nsis / winCodeSign 超时。
//
// 用法：npm run dist
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import https from 'node:https'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

const MIRROR =
  process.env.ELECTRON_BUILDER_BINARIES_MIRROR ||
  'https://registry.npmmirror.com/-/binary/electron-builder-binaries/'

const env = { ...process.env, ELECTRON_BUILDER_BINARIES_MIRROR: MIRROR }

function run(label, args) {
  console.log(`\n[dist] ${label}`)
  const result = spawnSync(process.execPath, args, { stdio: 'inherit', cwd: root, env })
  if (result.status !== 0) {
    console.error(`[dist] ${label} 失败（退出码 ${result.status}）`)
    process.exit(result.status || 1)
  }
}

// 带重定向跟随的下载（镜像站通常 302 到 CDN）
function download(url, dest, redirects = 5) {
  return new Promise((resolve, reject) => {
    https
      .get(url, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume()
          if (redirects <= 0) return reject(new Error(`重定向次数过多：${url}`))
          return resolve(download(new URL(res.headers.location, url).href, dest, redirects - 1))
        }
        if (res.statusCode !== 200) {
          res.resume()
          return reject(new Error(`下载失败 HTTP ${res.statusCode}：${url}`))
        }
        const file = fs.createWriteStream(dest)
        res.pipe(file)
        file.on('finish', () => file.close(() => resolve()))
        file.on('error', reject)
        res.on('error', reject)
      })
      .on('error', reject)
  })
}

// 准备 rcedit-x64.exe（供 afterPack 钩子写图标/版本信息用）
// 背景：electron-builder 内置 rcedit 走 winCodeSign 压缩包，包内含 Unix 软链接，
//       整包解压需要「创建符号链接」权限（Windows 需开发者模式或管理员）。
//       这里改成只抽取 rcedit-x64.exe 单文件，不触碰软链接，无需任何特权。
// 注：build/ 已被 gitignore，故本步骤保证 rcedit 可复现。
async function ensureRcedit() {
  const toolDir = path.join(root, 'build', 'tools')
  const rceditExe = path.join(toolDir, 'rcedit-x64.exe')
  if (fs.existsSync(rceditExe)) return

  fs.mkdirSync(toolDir, { recursive: true })
  const archive = path.join(toolDir, 'winCodeSign-2.6.0.7z')
  if (!fs.existsSync(archive)) {
    const url = `${MIRROR}winCodeSign-2.6.0/winCodeSign-2.6.0.7z`
    console.log(`\n[dist] 下载 rcedit 依赖 (winCodeSign)\n       ${url}`)
    await download(url, archive)
  }

  const sevenZip = path.join(root, 'node_modules', '7zip-bin', 'win', 'x64', '7za.exe')
  if (!fs.existsSync(sevenZip)) {
    throw new Error(`未找到 7za.exe：${sevenZip}`)
  }
  console.log('\n[dist] 抽取 rcedit-x64.exe')
  const result = spawnSync(
    sevenZip,
    ['e', archive, 'rcedit-x64.exe', `-o${toolDir}`, '-y', '-bd'],
    { stdio: 'inherit' }
  )
  if (result.status !== 0 || !fs.existsSync(rceditExe)) {
    throw new Error('抽取 rcedit-x64.exe 失败')
  }
}

// 1. 准备 rcedit（electron-builder 打包时 afterPack 钩子需要）
await ensureRcedit()
// 2. 构建前端（输出 dist/）
run('构建前端 (vite build)', [path.join(root, 'node_modules', 'vite', 'bin', 'vite.js'), 'build'])
// 3. 冻结后端（输出 build/pyinstaller/backend/backend.exe）
run('冻结后端 (PyInstaller)', [path.join(root, 'scripts', 'build-backend.mjs')])
// 4. 打包桌面应用（输出 release/）
run('打包应用 (electron-builder)', [
  path.join(root, 'node_modules', 'electron-builder', 'cli.js'),
  '--win',
  'nsis'
])

console.log('\n[dist] 完成，安装包位于 release/ 目录')

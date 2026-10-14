// electron-builder afterPack 钩子（阶段8 补）：给打包后的 exe 写入应用图标与版本信息
//
// 为什么不用 electron-builder 内置能力：其内置 rcedit 依赖 winCodeSign 压缩包，
// 而该压缩包内含 Unix 软链接，解压需要「创建符号链接」权限（Windows 需开发者模式或管理员）。
// 这里改为自行提取 rcedit-x64.exe：用 7za 只抽取单个文件，不触碰软链接，无需任何特权。
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== 'win32') return

  const root = path.resolve(__dirname, '..')
  const rcedit = path.join(root, 'build', 'tools', 'rcedit-x64.exe')
  const icon = path.join(root, 'assets', 'icon.ico')
  if (!fs.existsSync(rcedit)) {
    throw new Error(`未找到 rcedit-x64.exe：${rcedit}（请先执行 npm run dist，会自动准备）`)
  }
  if (!fs.existsSync(icon)) {
    throw new Error(`未找到应用图标：${icon}`)
  }

  const info = context.packager.appInfo
  const exe = path.join(context.appOutDir, `${info.productFilename}.exe`)
  const version4 = `${info.version}.0` // rcedit 需要四段式版本号

  const args = [
    exe,
    '--set-icon', icon,
    '--set-version-string', 'FileDescription', info.productName,
    '--set-version-string', 'ProductName', info.productName,
    '--set-version-string', 'CompanyName', info.companyName || '',
    '--set-version-string', 'LegalCopyright', info.copyright || '',
    '--set-file-version', version4,
    '--set-product-version', version4
  ]
  execFileSync(rcedit, args, { stdio: 'inherit' })
  console.log(`[after-pack] 已写入图标与版本信息：${path.basename(exe)}（${version4}）`)
}

// 开发启动脚本：先起 Vite dev server，再拉起 Electron，无需额外依赖
// 关闭 Electron 窗口后同步关闭 dev server
import { createServer } from 'vite'
import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import electronPath from 'electron'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')

const server = await createServer({
  configFile: path.join(root, 'vite.config.js'),
  root
})
await server.listen()

const port = server.config.server.port
const url = `http://localhost:${port}`
server.printUrls()

const child = spawn(electronPath, ['.'], {
  cwd: root,
  stdio: 'inherit',
  env: { ...process.env, VITE_DEV_SERVER_URL: url }
})

child.on('close', async () => {
  await server.close()
  process.exit(0)
})

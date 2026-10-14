import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import './style.css'

// 渲染进程入口：挂载 Vue3 应用并全局注册 Element Plus
createApp(App).use(ElementPlus).mount('#app')

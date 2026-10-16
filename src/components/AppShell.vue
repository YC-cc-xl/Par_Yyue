<template>
  <el-container class="app-shell">
    <!-- ================= 左侧导航栏 ================= -->
    <el-aside width="170px" class="app-aside">
      <div class="brand">离线工具箱</div>
      <el-menu :default-active="activeKey" class="app-menu" @select="handleSelect">
        <el-menu-item index="compose">自动合成</el-menu-item>
        <el-menu-item index="text">文本处理</el-menu-item>
      </el-menu>
    </el-aside>

    <el-container class="app-right" direction="vertical">
      <!-- ================= 全局顶栏（右上角占位按钮） ================= -->
      <div class="app-topbar">
        <span class="topbar-tip">纯离线本地运行 · 不联网</span>
        <div class="topbar-actions">
          <el-button size="small" plain @click="notImplemented('离线永久授权')">离线永久授权</el-button>
          <el-button size="small" plain @click="notImplemented('夜间模式')">夜间模式</el-button>
        </div>
      </div>

      <!-- ================= 页面容器 =================
           v-show（非 v-if）：切页不销毁组件——
             · 自动合成：任务轮询与表单状态保留
             · 文本处理：已打开/已编辑的文本内容保留 -->
      <el-main class="app-main">
        <AutoCompose v-show="activeKey === 'compose'" />
        <TextProcess v-show="activeKey === 'text'" />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
// 导航外壳：无 vue-router，沿用单页面内状态切换的既有组织方式
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import AutoCompose from './AutoCompose.vue'
import TextProcess from './TextProcess.vue'

// 当前激活页面：compose = 自动合成，text = 文本处理
const activeKey = ref('compose')

function handleSelect(key) {
  activeKey.value = key
}

// 「离线永久授权」「夜间模式」本期只做按钮占位，功能留待后续专项实现
function notImplemented(name) {
  ElMessage.info(`${name} 暂未实现，敬请期待`)
}
</script>

<style scoped>
.app-shell {
  height: 100%;
}

/* 左侧导航 */
.app-aside {
  display: flex;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid #ebeef5;
}
.brand {
  flex: none;
  height: 48px;
  display: flex;
  align-items: center;
  padding: 0 16px;
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  border-bottom: 1px solid #ebeef5;
}
.app-menu {
  flex: 1;
  border-right: none;
}

/* 右侧：顶栏 + 页面区 */
.app-right {
  min-width: 0;
}
.app-topbar {
  flex: none;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
}
.topbar-tip {
  font-size: 12px;
  color: #909399;
}
.topbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 页面区：去掉 el-main 默认内边距，交给各页面自己排版 */
.app-main {
  flex: 1;
  min-height: 0;
  padding: 0;
  overflow: hidden;
  background: #f5f7fa;
}
</style>

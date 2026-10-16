<template>
  <!-- 应用外壳：左侧导航（自动合成 / 文本处理）+ 页面切换
       说明：页面用 v-show 切换而非 v-if，切换时组件不被销毁，
             自动合成的轮询状态与文本处理的编辑内容都会保留 -->
  <AppShell />
</template>

<script setup>
import AppShell from './components/AppShell.vue'
</script>

<style scoped>
</style>

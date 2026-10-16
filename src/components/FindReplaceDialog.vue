<template>
  <el-dialog
    :model-value="visible"
    title="查找和替换"
    width="460px"
    append-to-body
    :close-on-click-modal="false"
    @update:model-value="(v) => emit('update:visible', v)"
  >
    <!-- 查找内容 -->
    <div class="dialog-row">
      <el-input
        ref="findInputRef"
        :model-value="findText"
        placeholder="输入要查找的文字"
        clearable
        @update:model-value="(v) => emit('update:findText', v)"
        @keyup.enter="emit('find-next')"
      />
    </div>

    <!-- 替换为（留空表示删除匹配文字） -->
    <div class="dialog-row mt8">
      <el-input
        :model-value="replaceText"
        placeholder="留空表示删除匹配文字"
        clearable
        @update:model-value="(v) => emit('update:replaceText', v)"
      />
    </div>

    <!-- 匹配统计：由父组件（持有编辑器全文）算好传入 -->
    <div class="hint-text">
      共匹配 {{ matchTotal }} 处<span v-if="currentIndex >= 0">，当前第 {{ currentIndex + 1 }} 处</span>
    </div>

    <template #footer>
      <el-button @click="emit('find-next')">查找下一个</el-button>
      <el-button @click="emit('replace-current')">替换当前</el-button>
      <el-button type="primary" @click="emit('replace-all')">全部替换</el-button>
      <!-- 关闭只收起弹窗：编辑器高亮与左侧搜索结果列表都会保留 -->
      <el-button @click="emit('update:visible', false)">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
// 查找替换模态弹窗（纯 UI 组件）：
//   本组件不接触文本内容，所有查找/替换都由父组件在本地完成，
//   通过事件把用户动作抛给父组件，父组件回传匹配总数与当前序号
import { nextTick, ref, watch } from 'vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  findText: { type: String, default: '' },
  replaceText: { type: String, default: '' },
  matchTotal: { type: Number, default: 0 },
  currentIndex: { type: Number, default: -1 }
})

const emit = defineEmits([
  'update:visible',
  'update:findText',
  'update:replaceText',
  'find-next',
  'replace-current',
  'replace-all'
])

const findInputRef = ref(null)

// 弹窗打开后自动聚焦「查找内容」输入框，便于直接输入
watch(
  () => props.visible,
  (open) => {
    if (open) nextTick(() => findInputRef.value && findInputRef.value.focus && findInputRef.value.focus())
  }
)
</script>

<style scoped>
.dialog-row {
  display: flex;
  align-items: center;
}
.mt8 {
  margin-top: 8px;
}
.hint-text {
  margin-top: 10px;
  font-size: 12px;
  color: #a8abb2;
  line-height: 1.6;
}
</style>

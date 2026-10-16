<template>
  <div class="search-panel">
    <!-- 匹配条目：序号 / 行号 / 所在内容；点击某条 → 编辑器跳转并高亮 -->
    <el-table
      v-if="rows.length"
      :data="rows"
      size="small"
      border
      stripe
      height="420"
      :row-class-name="rowClassName"
      @row-click="handleRowClick"
    >
      <el-table-column prop="no" label="序号" width="64" align="center" />
      <el-table-column prop="line" label="行号" width="76" align="center" />
      <el-table-column prop="text" label="所在内容" min-width="140" show-overflow-tooltip />
    </el-table>

    <el-empty v-else :description="`${total} 处匹配`" :image-size="60" />

    <!-- 大文本安全提示：只渲染前 N 条，避免一次性创建上万行 DOM -->
    <div v-if="rows.length && total > rows.length" class="hint-text">
      匹配过多，仅显示前 {{ rows.length }} 条（共 {{ total }} 处）
    </div>
  </div>
</template>

<script setup>
// 搜索结果面板（纯展示组件）：行数据由父组件算好传入，这里只负责渲染与点击派发
const props = defineProps({
  // 行数据：[{ no: 1 基序号, line: 行号, text: 该行内容 }]
  rows: { type: Array, default: () => [] },
  // 匹配总数（可能大于 rows.length，因为只渲染前 N 条）
  total: { type: Number, default: 0 },
  // 当前激活的匹配序号（1 基），用于高亮当前行
  activeNo: { type: Number, default: 0 }
})

const emit = defineEmits(['select'])

// 当前匹配行加高亮样式
function rowClassName({ row }) {
  return row.no === props.activeNo ? 'active-match-row' : ''
}

// 点击行 → 把「序号」抛给父组件（父组件据此定位并高亮编辑器中的匹配）
function handleRowClick(row) {
  emit('select', row.no)
}
</script>

<style scoped>
.search-panel {
  min-height: 420px;
}

/* 当前匹配行底色（Element Plus 表格行需要 :deep 穿透） */
.search-panel :deep(.active-match-row) {
  background: #fff8e1 !important;
}
.search-panel :deep(.active-match-row td) {
  background: #fff8e1 !important;
  font-weight: 600;
}

.hint-text {
  margin-top: 8px;
  font-size: 12px;
  color: #a8abb2;
  line-height: 1.6;
}
</style>

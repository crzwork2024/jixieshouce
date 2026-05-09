<template>
  <div class="toc-panel">
    <div class="toc-header">
      <el-icon><Document /></el-icon>
      <span>文档目录</span>
      <el-tag size="small" type="info" style="margin-left:auto">{{ toc.length }}</el-tag>
    </div>

    <div class="toc-search">
      <el-input
        v-model="searchText"
        placeholder="搜索章节..."
        size="small"
        clearable
        :prefix-icon="Search"
      />
    </div>

    <div class="toc-list" v-if="filteredToc.length">
      <div
        v-for="item in filteredToc"
        :key="item.toc_id"
        class="toc-item"
        :class="{ active: activeTocId === item.toc_id }"
        :style="{ paddingLeft: `${(item.level - 1) * 14 + 8}px` }"
        @click="handleClick(item)"
        :title="item.title"
      >
        <span class="toc-dot" :style="{ background: levelColors[item.level] || '#4361ee' }" />
        <span class="toc-title">{{ item.title }}</span>
        <span class="toc-page">P{{ item.page_idx + 1 }}</span>
      </div>
    </div>

    <el-empty v-else description="暂无目录数据" :image-size="60" />
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { Document, Search } from '@element-plus/icons-vue'

const props = defineProps({
  toc:         { type: Array,  default: () => [] },
  activeTocId: { type: String, default: '' },
})

const emit = defineEmits(['select'])

const searchText = ref('')

const levelColors = {
  1: '#2563eb',
  2: '#7c3aed',
  3: '#059669',
  4: '#d97706',
}

const filteredToc = computed(() => {
  if (!searchText.value.trim()) return props.toc
  const kw = searchText.value.trim().toLowerCase()
  return props.toc.filter(t => t.title.toLowerCase().includes(kw))
})

function handleClick(item) {
  emit('select', item)
}
</script>

<style scoped>
.toc-panel {
  display: flex; flex-direction: column; height: 100%;
  background: #fff; border-right: 1px solid #e4e7ed;
}
.toc-header {
  display: flex; align-items: center; gap: 6px;
  padding: 12px 14px; font-size: 14px; font-weight: 600; color: #1d3557;
  border-bottom: 1px solid #eef0f4; flex-shrink: 0;
}
.toc-search { padding: 8px 10px; flex-shrink: 0; border-bottom: 1px solid #f0f0f0; }
.toc-list { flex: 1; overflow-y: auto; padding: 6px 0; }

.toc-item {
  display: flex; align-items: center; padding: 5px 8px;
  border-radius: 5px; cursor: pointer; transition: all 0.15s;
  font-size: 13px; color: #374151; line-height: 1.4; gap: 6px;
  margin: 1px 6px;
}
.toc-item:hover { background: #eef2ff; color: #4361ee; }
.toc-item.active { background: #dde9ff; color: #2c4ecf; font-weight: 600; }
.toc-dot {
  width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
}
.toc-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.toc-page { font-size: 11px; color: #9ca3af; white-space: nowrap; flex-shrink: 0; }
</style>

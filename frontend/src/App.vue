<template>
  <div class="app-layout">
    <!-- ── 顶部导航栏 ── -->
    <header class="app-header">
      <div class="header-left">
        <el-icon :size="22" color="#4361ee"><Reading /></el-icon>
        <span class="app-title">机械手册 RAG 智能问答</span>
      </div>
      <div class="header-center">
        <el-select
          v-model="selectedPdfId"
          placeholder="选择 PDF 文档"
          size="small"
          style="width: 240px"
          clearable
          @change="onPdfChange"
        >
          <el-option
            v-for="pdf in pdfList"
            :key="pdf.id"
            :label="pdf.indexed ? pdf.name : `${pdf.name}（未索引）`"
            :value="pdf.id"
            :disabled="!pdf.indexed"
          />
        </el-select>
      </div>
      <div class="header-right">
        <el-button size="small" :icon="Upload" @click="showIndexDialog = true">
          导入文档
        </el-button>
        <el-button size="small" :icon="DataAnalysis" @click="fetchStats">统计</el-button>
      </div>
    </header>

    <!-- ── 主体三栏布局 ── -->
    <div class="app-body">
      <!-- 左侧目录 -->
      <aside class="sidebar-toc" :class="{ collapsed: tocCollapsed }">
        <TocPanel
          :toc="toc"
          :active-toc-id="activeTocId"
          @select="onTocSelect"
        />
        <div class="toc-toggle" @click="tocCollapsed = !tocCollapsed">
          <el-icon><component :is="tocCollapsed ? ArrowRight : ArrowLeft" /></el-icon>
        </div>
      </aside>

      <!-- 中间 PDF 阅读器 -->
      <main class="pdf-area" v-show="pdfUrl">
        <PdfViewer
          ref="pdfViewerRef"
          :pdf-url="pdfUrl"
          :highlights="currentHighlights"
          :target-page="jumpToPage"
          @page-change="onPageChange"
        />
      </main>
      <div v-if="!pdfUrl" class="pdf-placeholder">
        <el-icon :size="64" color="#dce0e8"><Document /></el-icon>
        <p>请先选择 PDF 文档或导入数据</p>
      </div>

      <!-- 右侧问答面板 -->
      <aside class="sidebar-chat">
        <ChatPanel
          ref="chatPanelRef"
          :pdf-file-id="selectedPdfId"
          @highlight="onHighlight"
          @jump-pdf="onJumpPdf"
          @send="q => chatPanelRef?.sendQuery(q)"
        />
      </aside>
    </div>

    <!-- ── 导入对话框 ── -->
    <el-dialog
      v-model="showIndexDialog"
      title="导入 MinerU 解析文档"
      width="500px"
      :close-on-click-modal="false"
    >
      <el-form :model="indexForm" label-width="100px" size="default">
        <el-form-item label="数据目录">
          <el-input
            v-model="indexForm.input_dir"
            placeholder="默认 ./data，可填绝对路径"
          />
          <div class="form-hint">留空则使用 <code>./data</code>，系统自动探测 PDF ID</div>
        </el-form-item>
        <el-form-item label="PDF ID">
          <el-input v-model="indexForm.pdf_id" placeholder="可选，留空则自动探测" />
          <div class="form-hint">自动从目录中找 <code>*_origin.pdf</code> 推断 ID</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showIndexDialog = false">取消</el-button>
        <el-button type="primary" :loading="indexing" @click="triggerIndex">
          开始处理 & 索引
        </el-button>
      </template>
    </el-dialog>

    <!-- ── 统计对话框 ── -->
    <el-dialog v-model="showStats" title="向量库统计" width="360px">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="目录节点数">{{ stats.toc_count ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="语义块总数">{{ stats.blocks_count ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="持久化目录">{{ stats.persist_dir ?? '-' }}</el-descriptions-item>
      </el-descriptions>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, nextTick } from 'vue'
import {
  Reading, Upload, DataAnalysis, Document,
  ArrowLeft, ArrowRight,
} from '@element-plus/icons-vue'
import { ElMessage, ElNotification } from 'element-plus'
import axios from 'axios'
import PdfViewer from './components/PdfViewer.vue'
import TocPanel  from './components/TocPanel.vue'
import ChatPanel from './components/ChatPanel.vue'

// ── 状态 ─────────────────────────────────
const pdfViewerRef = ref(null)
const chatPanelRef = ref(null)

const selectedPdfId    = ref(null)
const pdfUrl           = ref('')
const toc              = ref([])
const activeTocId      = ref('')
const currentHighlights = ref([])
const jumpToPage       = ref(null)
const tocCollapsed     = ref(false)

const pdfList = ref([])

// 导入对话框
const showIndexDialog = ref(false)
const indexing = ref(false)
const indexForm = ref({ pdf_id: '', input_dir: '' })

// 统计
const showStats = ref(false)
const stats = ref({})

// ── 生命周期 ──────────────────────────────
onMounted(async () => {
  await fetchPdfList()
  await fetchToc()
})

// ── PDF 列表加载 ──────────────────────────
async function fetchPdfList() {
  try {
    const { data } = await axios.get('/api/pdfs')
    pdfList.value = (data.pdfs || []).map(p => ({
      id:      p.id,
      name:    p.name,
      indexed: p.indexed,
    }))
    // 默认选中第一个已索引的文档
    const first = pdfList.value.find(p => p.indexed) || pdfList.value[0]
    if (first) {
      selectedPdfId.value = first.id
      onPdfChange(first.id)
    }
  } catch (e) {
    console.warn('PDF 列表加载失败:', e.message)
  }
}

// ── PDF 切换 ──────────────────────────────
function onPdfChange(id) {
  if (!id) { pdfUrl.value = ''; return }
  // 后端 /pdf/ 路由提供 PDF 文件（按文件名）
  pdfUrl.value = `/pdf/${id}_origin.pdf`
  currentHighlights.value = []
  fetchToc(id)
}

// ── 目录加载 ──────────────────────────────
async function fetchToc(pdfFileId) {
  try {
    const params = pdfFileId ? { pdf_file_id: pdfFileId } : {}
    const { data } = await axios.get('/api/toc', { params })
    toc.value = data.toc || []
  } catch (e) {
    console.warn('目录加载失败:', e.message)
  }
}

// ── 目录点击跳转 ──────────────────────────
function onTocSelect(item) {
  activeTocId.value = item.toc_id
  const page = (item.page_idx || 0) + 1
  jumpToPage.value = page

  // 高亮目录对应 bbox
  if (item.bbox && item.bbox.length === 4) {
    currentHighlights.value = [{
      page_idx: item.page_idx,
      bbox:     item.bbox,
    }]
  }
}

// ── PDF 高亮（来自问答引用） ──────────────
function onHighlight(ref) {
  currentHighlights.value = (ref.bboxes || []).map(b => ({
    page_idx: b.page_idx,
    bbox:     b.bbox,
  }))
  const page = currentHighlights.value.length
    ? currentHighlights.value[0].page_idx + 1
    : (ref.page_range?.[0] ?? 0) + 1
  _triggerJump(page)
}

function onJumpPdf(ref) {
  if (ref.page_range && ref.page_range.length) {
    _triggerJump(ref.page_range[0] + 1)
  }
}

// 先置 null 再设值，确保即使页码相同 watcher 也会重触发
function _triggerJump(page) {
  jumpToPage.value = null
  nextTick(() => { jumpToPage.value = page })
}

function onPageChange(pageNum) {
  // 同步左侧目录高亮：找到最接近当前页的目录项
  const nearest = toc.value.reduce((best, item) => {
    if (item.page_idx > pageNum - 1) return best
    if (!best || item.page_idx > best.page_idx) return item
    return best
  }, null)
  if (nearest) activeTocId.value = nearest.toc_id
}

// ── 触发索引 ──────────────────────────────
async function triggerIndex() {
  indexing.value = true
  try {
    const { data } = await axios.post('/api/index', indexForm.value)
    ElNotification({
      title: '索引构建完成',
      message: `语义块: ${data.vector_stats?.blocks_count}，目录: ${data.vector_stats?.toc_count}`,
      type: 'success',
    })
    showIndexDialog.value = false
    indexForm.value = { pdf_id: '', input_dir: '' }
    // 刷新 PDF 列表和目录
    await fetchPdfList()
    await fetchToc()
  } catch (e) {
    ElMessage.error(`索引失败: ${e.response?.data?.detail || e.message}`)
  } finally {
    indexing.value = false
  }
}

// ── 统计 ──────────────────────────────────
async function fetchStats() {
  try {
    const { data } = await axios.get('/api/stats')
    stats.value = data
    showStats.value = true
  } catch (e) {
    ElMessage.error('获取统计失败')
  }
}
</script>

<style scoped>
.app-layout { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

/* 顶部导航栏 */
.app-header {
  display: flex; align-items: center; padding: 0 16px; height: 52px;
  background: #fff; border-bottom: 1px solid #e4e7ed; flex-shrink: 0;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
.header-left { display: flex; align-items: center; gap: 8px; flex: 1; }
.app-title { font-size: 16px; font-weight: 700; color: #1d3557; }
.header-center { display: flex; justify-content: center; flex: 1; }
.header-right { display: flex; align-items: center; gap: 8px; flex: 1; justify-content: flex-end; }

/* 主体三栏 */
.app-body { display: flex; flex: 1; overflow: hidden; }

/* 左侧目录 */
.sidebar-toc {
  width: 240px; flex-shrink: 0; position: relative;
  transition: width 0.25s ease; overflow: hidden;
}
.sidebar-toc.collapsed { width: 0; }
.toc-toggle {
  position: absolute; right: -14px; top: 50%;
  transform: translateY(-50%); width: 14px; height: 48px;
  background: #e4e7ed; border-radius: 0 6px 6px 0;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; z-index: 10; font-size: 12px; color: #666;
}
.toc-toggle:hover { background: #c8cdd6; }

/* 中间 PDF */
.pdf-area {
  flex: 1; min-width: 0; overflow: hidden; border-right: 1px solid #e4e7ed;
}
.pdf-placeholder {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 12px; background: #f5f7fa; color: #9ca3af;
  font-size: 14px;
}

/* 右侧问答 */
.sidebar-chat {
  width: 420px; flex-shrink: 0; background: #fff;
  display: flex; flex-direction: column; overflow: hidden;
}

/* 表单提示 */
.form-hint { font-size: 12px; color: #9ca3af; margin-top: 4px; }
</style>

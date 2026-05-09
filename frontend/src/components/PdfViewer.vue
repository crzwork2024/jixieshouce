<template>
  <div class="pdf-viewer-root" ref="rootEl">
    <!-- 工具栏 -->
    <div class="pdf-toolbar">
      <el-button-group size="small">
        <el-button :icon="ArrowLeft" @click="prevPage" :disabled="currentPage <= 1" />
        <el-button :icon="ArrowRight" @click="nextPage" :disabled="currentPage >= totalPages" />
      </el-button-group>
      <span class="page-info">
        <el-input-number
          v-model="currentPage"
          :min="1" :max="Math.max(1, totalPages)"
          :disabled="totalPages === 0"
          size="small" style="width:72px"
          @change="jumpToPage"
        /> / {{ totalPages || '-' }}
      </span>
      <el-button-group size="small" style="margin-left:8px">
        <el-button :icon="ZoomOut" @click="zoomOut" />
        <el-button :icon="ZoomIn"  @click="zoomIn" />
      </el-button-group>
      <span class="zoom-label">{{ Math.round(scale * 100) }}%</span>
      <el-button size="small" :icon="Refresh" @click="resetZoom" style="margin-left:4px" />
    </div>

    <!-- 画布区 -->
    <div class="pdf-scroll-area" ref="scrollArea">
      <div
        v-for="p in renderedPages"
        :key="p.pageNum"
        class="pdf-page-wrap"
        :ref="el => setPageRef(el, p.pageNum)"
        :data-page="p.pageNum"
      >
        <canvas :ref="el => setCanvasRef(el, p.pageNum)" />
        <!-- 高亮层 -->
        <div class="pdf-highlight-layer" :ref="el => setHighlightRef(el, p.pageNum)" />
      </div>
    </div>

    <!-- 加载中 -->
    <div v-if="loading" class="pdf-loading">
      <el-icon class="is-loading" :size="32"><Loading /></el-icon>
      <span>加载中...</span>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ArrowLeft, ArrowRight, ZoomIn, ZoomOut, Refresh, Loading } from '@element-plus/icons-vue'

import * as pdfjsLib from 'pdfjs-dist'
// 使用本地 node_modules worker，不依赖网络
pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url
).href

const props = defineProps({
  pdfUrl:     { type: String, default: '' },
  highlights: { type: Array,  default: () => [] }, // [{ page_idx, bbox:[x1,y1,x2,y2] }, ...]
  targetPage: { type: Number, default: null },      // 跳转目标页（1-based）
})

const emit = defineEmits(['page-change'])

// ── 状态 ──────────────────────────────────────
const rootEl    = ref(null)
const scrollArea = ref(null)
const loading   = ref(false)
const scale     = ref(1.4)
const currentPage  = ref(1)
const totalPages   = ref(0)

// page canvas/highlight/wrap refs（map: pageNum -> element）
const canvasRefs    = {}
const highlightRefs = {}
const pageWrapRefs  = {}

// pdf.js document & rendered page cache
let pdfDoc     = null
const renderedPages = ref([])   // [{pageNum}]
const renderQueue   = new Set()

// ── Ref 注册 ─────────────────────────────────
function setCanvasRef(el, num)    { if (el) canvasRefs[num] = el }
function setHighlightRef(el, num) { if (el) highlightRefs[num] = el }
function setPageRef(el, num)      { if (el) pageWrapRefs[num] = el }

// ── 加载 PDF ─────────────────────────────────
async function loadPdf(url) {
  if (!url) return
  loading.value = true
  renderedPages.value = []
  currentPage.value = 1
  totalPages.value   = 0

  try {
    pdfDoc = await pdfjsLib.getDocument({ url, cMapUrl: 'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.3.136/cmaps/', cMapPacked: true }).promise
    totalPages.value = pdfDoc.numPages

    // 先生成页面占位符
    renderedPages.value = Array.from({ length: pdfDoc.numPages }, (_, i) => ({ pageNum: i + 1 }))

    await nextTick()
    // 渲染第 1 页，其余惰性加载
    await renderPage(1)
    setupScrollObserver()
  } finally {
    loading.value = false
  }
}

// ── 渲染单页 ─────────────────────────────────
async function renderPage(pageNum) {
  if (renderQueue.has(pageNum) || !pdfDoc) return
  renderQueue.add(pageNum)

  const page     = await pdfDoc.getPage(pageNum)
  const viewport = page.getViewport({ scale: scale.value })
  const canvas   = canvasRefs[pageNum]
  if (!canvas) { renderQueue.delete(pageNum); return }

  canvas.width  = viewport.width
  canvas.height = viewport.height
  canvas.style.display = 'block'

  await page.render({
    canvasContext: canvas.getContext('2d'),
    viewport,
  }).promise

  renderQueue.delete(pageNum)
  applyHighlightsForPage(pageNum)
}

// ── 高亮绘制 ─────────────────────────────────
function applyHighlightsForPage(pageNum) {
  const hl = highlightRefs[pageNum]
  const cv = canvasRefs[pageNum]
  if (!hl || !cv) return

  // 清除旧高亮
  hl.innerHTML = ''

  const matching = (props.highlights || []).filter(h => h.page_idx === pageNum - 1)
  if (!matching.length) return

  const W = cv.width  / scale.value  // 原始页面宽度
  const H = cv.height / scale.value  // 原始页面高度
  const dpr = window.devicePixelRatio || 1

  matching.forEach(h => {
    const [nx1, ny1, nx2, ny2] = h.bbox   // 归一化 0-1
    const rect = document.createElement('div')
    rect.className = 'pdf-highlight-rect'
    // canvas 实际 CSS 尺寸
    const cssW = cv.offsetWidth
    const cssH = cv.offsetHeight
    rect.style.left   = `${nx1 * cssW}px`
    rect.style.top    = `${ny1 * cssH}px`
    rect.style.width  = `${(nx2 - nx1) * cssW}px`
    rect.style.height = `${(ny2 - ny1) * cssH}px`
    hl.appendChild(rect)
  })
}

function clearHighlights() {
  Object.values(highlightRefs).forEach(el => { if (el) el.innerHTML = '' })
}

function applyAllHighlights() {
  Object.keys(canvasRefs).forEach(num => applyHighlightsForPage(Number(num)))
}

// ── 惰性加载（Intersection Observer）────────
let observer = null
function setupScrollObserver() {
  if (observer) observer.disconnect()
  observer = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const num = Number(entry.target.dataset.page)
          if (num && !renderQueue.has(num)) {
            renderPage(num)
            const visible = Math.round(
              (entry.target.offsetTop + entry.target.offsetHeight / 2) /
              scrollArea.value.scrollHeight * totalPages.value
            )
            if (visible) { currentPage.value = num; emit('page-change', num) }
          }
        }
      })
    },
    { root: scrollArea.value, threshold: 0.1 }
  )
  Object.values(pageWrapRefs).forEach(el => { if (el) observer.observe(el) })
}

// ── 缩放 ─────────────────────────────────────
async function zoomIn()   { scale.value = Math.min(scale.value + 0.2, 3.0); await rerenderAll() }
async function zoomOut()  { scale.value = Math.max(scale.value - 0.2, 0.5); await rerenderAll() }
async function resetZoom(){ scale.value = 1.4; await rerenderAll() }

async function rerenderAll() {
  renderQueue.clear()
  clearHighlights()
  const visible = [...Object.keys(canvasRefs)].slice(0, 5).map(Number)
  for (const num of visible) await renderPage(num)
  applyAllHighlights()
}

// ── 翻页/跳转 ────────────────────────────────
function prevPage() { if (currentPage.value > 1) jumpToPage(currentPage.value - 1) }
function nextPage() { if (currentPage.value < totalPages.value) jumpToPage(currentPage.value + 1) }

async function jumpToPage(num) {
  if (!num || num < 1) return
  currentPage.value = num
  // 先确保该页已渲染
  await renderPage(num)
  await nextTick()
  // 在自定义滚动容器内用 scrollTo，比 scrollIntoView 更可靠
  const wrap = pageWrapRefs[num]
  if (wrap && scrollArea.value) {
    scrollArea.value.scrollTo({ top: wrap.offsetTop - 16, behavior: 'smooth' })
  }
  emit('page-change', num)
}

// ── 公开方法（供父组件调用）──────────────────
defineExpose({
  jumpToPage,
  applyHighlightsForPage,
  applyAllHighlights,
  clearHighlights,
})

// ── 监听 props 变化 ───────────────────────────
watch(() => props.pdfUrl,     url  => loadPdf(url))
watch(() => props.highlights, ()   => nextTick(applyAllHighlights), { deep: true })
watch(() => props.targetPage, page => { if (page && page > 0) jumpToPage(page) })

onMounted(() => { if (props.pdfUrl) loadPdf(props.pdfUrl) })
onBeforeUnmount(() => { if (observer) observer.disconnect() })
</script>

<style scoped>
.pdf-viewer-root {
  display: flex; flex-direction: column; height: 100%; background: #525659;
}
.pdf-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; background: #3c3f41; flex-shrink: 0;
  border-bottom: 1px solid #222;
}
.page-info { color: #ccc; font-size: 13px; display: flex; align-items: center; gap: 6px; }
.zoom-label { color: #ccc; font-size: 12px; min-width: 40px; }

.pdf-scroll-area {
  flex: 1; overflow-y: auto; padding: 16px 0;
  display: flex; flex-direction: column; align-items: center; gap: 12px;
}
.pdf-page-wrap {
  position: relative; box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  background: #fff;
}
.pdf-page-wrap canvas { display: block; }
.pdf-highlight-layer {
  position: absolute; top: 0; left: 0; width: 100%; height: 100%;
  pointer-events: none; z-index: 10;
}

.pdf-loading {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 12px;
  background: rgba(82,86,89,0.85); color: #fff; font-size: 14px;
}
</style>

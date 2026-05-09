<template>
  <div class="chat-panel">
    <!-- 消息列表 -->
    <div class="messages-area" ref="messagesEl">
      <!-- 空状态 -->
      <div v-if="!messages.length" class="empty-state">
        <el-icon :size="48" color="#c0c4cc"><ChatDotRound /></el-icon>
        <p>输入问题，从机械手册中检索答案</p>
        <div class="example-queries">
          <el-tag
            v-for="q in exampleQueries" :key="q"
            type="info" effect="plain" class="example-tag"
            @click="$emit('send', q)"
          >{{ q }}</el-tag>
        </div>
      </div>

      <!-- 消息列表 -->
      <template v-else>
        <div
          v-for="msg in messages"
          :key="msg.id"
          class="message-item"
          :class="msg.role"
        >
          <!-- 用户消息 -->
          <div v-if="msg.role === 'user'" class="user-bubble">
            <el-icon><User /></el-icon>
            <span>{{ msg.content }}</span>
          </div>

          <!-- 助手消息 -->
          <div v-else class="assistant-bubble">
            <div class="assistant-avatar">
              <el-icon><Service /></el-icon>
            </div>
            <div class="assistant-body">
              <!-- 流式加载动画 -->
              <div v-if="msg.streaming" class="streaming-indicator">
                <span class="dot" /><span class="dot" /><span class="dot" />
              </div>

              <!-- 回答正文（Markdown 渲染） -->
              <div
                v-else
                class="answer-content"
                v-html="renderAnswer(msg.content, msg.references)"
                @click="handleAnswerClick($event, msg)"
              />

              <!-- 参考文献区 -->
              <div v-if="msg.references && msg.references.length" class="refs-section">
                <div class="refs-title">
                  <el-icon><Connection /></el-icon> 参考来源
                </div>
                <div
                  v-for="ref in msg.references"
                  :key="ref.index"
                  class="ref-card"
                  :class="{ active: activeRefIndex === ref.index && activeRefMsgId === msg.id }"
                  :id="`ref-${msg.id}-${ref.index}`"
                  @click="handleRefClick(ref, msg)"
                >
                  <div class="ref-header">
                    <span class="ref-index">{{ ref.index }}</span>
                    <span class="ref-chapter">{{ ref.chapter }}</span>
                    <span class="ref-page">{{ ref.page_label }}</span>
                    <el-tag
                      size="small"
                      :type="typeTagMap[ref.block_type] || 'info'"
                    >{{ typeLabel(ref.block_type) }}</el-tag>
                  </div>
                  <!-- 表格 / Markdown 图片混排 -->
                  <div
                    v-if="refNeedsRichPreview(ref)"
                    class="ref-preview ref-table-wrap ref-rich-wrap"
                    v-html="sanitizeRichPreview(ref.raw_content)"
                  />
                  <!-- 图片类型：渲染图片 -->
                  <div
                    v-else-if="isImageType(ref.block_type)"
                    class="ref-preview ref-image-wrap"
                  >
                    <img
                      v-for="(path, idx) in ref.img_paths"
                      :key="idx"
                      :src="resolveImageSrc(path)"
                      class="ref-img"
                      :alt="`图片 ${idx + 1}`"
                      loading="lazy"
                    />
                    <span v-if="!ref.img_paths || !ref.img_paths.length" class="ref-preview-text">[图片]</span>
                  </div>
                  <!-- 普通文本 -->
                  <div v-else class="ref-preview" v-html="sanitize(ref.preview)" />
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 加载占位 -->
        <div v-if="loading" class="message-item assistant">
          <div class="assistant-bubble">
            <div class="assistant-avatar"><el-icon><Service /></el-icon></div>
            <div class="assistant-body">
              <div class="streaming-indicator">
                <span class="dot" /><span class="dot" /><span class="dot" />
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- 输入区 -->
    <div class="input-area">
      <div class="query-input-wrap">
        <el-input
          v-model="inputText"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 5 }"
          placeholder="输入问题... (Ctrl+Enter 发送)"
          resize="none"
          @keydown.enter.exact.prevent="sendQuery"
        @keydown.ctrl.enter.prevent="sendQuery"
        />
        <el-button
          type="primary"
          class="query-send-btn"
          :loading="loading"
          @click="sendQuery"
          :icon="Promotion"
        >发送</el-button>
      </div>
      <div class="input-hint">
        <span>Enter 发送 · Shift+Enter 换行</span>
        <el-switch v-model="streamMode" size="small" active-text="流式" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick, watch } from 'vue'
import {
  ChatDotRound, User, Service, Connection, Promotion
} from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import axios from 'axios'

const props = defineProps({
  pdfFileId: { type: String, default: null },
})

const emit = defineEmits(['highlight', 'jump-pdf', 'send'])

// ── 状态 ──────────────────────────────────
const messages       = ref([])
const inputText      = ref('')
const loading        = ref(false)
const streamMode     = ref(true)
const messagesEl     = ref(null)
const activeRefIndex = ref(null)
const activeRefMsgId = ref(null)

const exampleQueries = [
  '什么是几何公差带？',
  '最大实体要求（MMR）的定义是什么？',
  '体外作用尺寸如何定义？',
  '独立原则和相关要求有什么区别？',
]

const typeTagMap = {
  table:      'warning',
  table_unit: 'warning',
  image:      'success',
  title:      'primary',
  section:    'primary',
  text:       'info',
  list:       'info',
}

// ── 工具 ──────────────────────────────────

/** 普通文本安全净化（不允许表格标签） */
function sanitize(html) {
  return DOMPurify.sanitize(html || '', {
    ALLOWED_TAGS: ['b', 'strong', 'em', 'code', 'br', 'span'],
    ALLOWED_ATTR: [],
  })
}

/** 表格 HTML 净化（允许 table + 内嵌 img，便于「表 + 示意图」混排） */
function sanitizeTable(html) {
  return DOMPurify.sanitize(html || '', {
    ALLOWED_TAGS: [
      'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td',
      'colgroup', 'col', 'caption',
      'b', 'strong', 'em', 'br', 'span', 'p', 'sup', 'sub',
      'img',
    ],
    ALLOWED_ATTR: [
      'colspan', 'rowspan', 'align', 'valign', 'style', 'class',
      'src', 'alt', 'loading',
    ],
    FORBID_ATTR: ['onclick'],
  })
}

function isImageType(t) {
  return t === 'image'
}

function typeLabel(t) {
  const map = {
    table:      '表格',
    table_unit: '表格单元',
    image:      '图片',
    title:      '标题',
    section:    '章节',
    text:       '文本',
    list:       '列表',
  }
  return map[t] || t
}

/** 统一转为前端请求的 /images/{文件名}，修正 images//、重复前缀等 */
function resolveImageSrc(stored) {
  let u = String(stored || '').trim().replace(/\\/g, '/')
  while (u.includes('//')) u = u.replace(/\/\//g, '/')
  u = u.replace(/^\/+/, '')
  if (u.toLowerCase().startsWith('images/')) {
    u = u.slice('images/'.length).replace(/^\/+/, '')
  }
  return `/images/${u}`
}

/** 将 Markdown 图片语法转为 <img>（路径经 resolveImageSrc） */
function markdownImagesToImgHtml(text) {
  return (text || '').replace(/!\[[^\]]*\]\(([^)]+)\)/g, (_, url) => {
    const src = resolveImageSrc(url.trim())
    return `<img src="${src}" class="ref-inline-img" alt="" loading="lazy"/>`
  })
}

/** 参考文献卡：表格 + Markdown 图混排 */
function sanitizeRichPreview(raw) {
  const html = markdownImagesToImgHtml(raw || '')
  const wrapped = `<div class="rich-ref-root">${html}</div>`
  return DOMPurify.sanitize(wrapped, {
    ALLOWED_TAGS: [
      'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td',
      'colgroup', 'col', 'caption',
      'b', 'strong', 'em', 'br', 'span', 'p', 'sup', 'sub',
      'img',
      'div',
    ],
    ALLOWED_ATTR: [
      'colspan', 'rowspan', 'align', 'valign', 'style', 'class',
      'src', 'alt', 'loading',
    ],
    FORBID_ATTR: ['onclick'],
  })
}

function refNeedsRichPreview(ref) {
  const raw = ref.raw_content || ''
  return ref.has_html_table || /\!\[[^\]]*\]\([^)]+\)/.test(raw)
}

function findRefForEmbed(references, bid) {
  if (!references?.length || bid == null || bid === '') return null
  let ref = references.find(r => r.block_id === bid)
  if (ref) return ref
  const b = String(bid).trim()
  ref = references.find(r => (r.raw_content || '').includes(b))
  if (ref) return ref
  if (b.length >= 16 && /^[a-fA-F0-9-]+$/.test(b.replace(/-/g, ''))) {
    const hex = b.replace(/-/g, '').toLowerCase()
    ref = references.find(r => (r.raw_content || '').toLowerCase().includes(hex))
  }
  return ref || null
}

/**
 * 将回答文本渲染为 HTML：
 * 1. Markdown 解析
 * 2. [N] → 可点击角标
 * 3. <!-- EMBED_TABLE:id --> → 实际表格 HTML
 * 4. <!-- EMBED_IMAGE:id --> → 实际图片
 */
function renderAnswer(content, references) {
  if (!content) return ''
  let html = marked.parse(content)

  // [N] → 可点击引用角标
  html = html.replace(/\[(\d+)\]/g, (_, n) => {
    const ref = (references || []).find(r => r.index === Number(n))
    const cls = (activeRefIndex.value === Number(n)) ? 'cite-badge active' : 'cite-badge'
    return `<span class="${cls}" data-ref="${n}" title="${ref ? ref.chapter : ''}">[${n}]</span>`
  })

  // <!-- EMBED_TABLE:id --> → 实际表格（可与 Markdown 图混排）
  html = html.replace(/<!--\s*EMBED_TABLE:([^\s>]+)\s*-->/g, (_, bid) => {
    const ref = findRefForEmbed(references, bid)
    if (!ref) return `<em>[表格 ${bid} 未找到]</em>`
    const inner = sanitizeRichPreview(ref.raw_content)
    return `<div class="inline-table-ref" data-ref="${ref.index}">
      <div class="inline-ref-label">&#128202; 表格引用 [${ref.index}] · ${ref.chapter} · ${ref.page_label}</div>
      <div class="inline-ref-content">${inner}</div>
    </div>`
  })

  // <!-- EMBED_IMAGE:id --> → 实际图片（block_id 或正文/文件名片段）
  html = html.replace(/<!--\s*EMBED_IMAGE:([^\s>]+)\s*-->/g, (_, bid) => {
    const ref = findRefForEmbed(references, bid)
    if (!ref) return `<em>[图片 ${bid} 未找到]</em>`
    let imgs = (ref.img_paths || [])
      .map(p => `<img src="${resolveImageSrc(p)}" class="inline-ref-img" alt="" loading="lazy"/>`)
      .join('')
    if (!imgs && ref.raw_content) {
      imgs = markdownImagesToImgHtml(ref.raw_content)
      imgs = DOMPurify.sanitize(imgs, {
        ALLOWED_TAGS: ['img'],
        ALLOWED_ATTR: ['src', 'alt', 'class', 'loading'],
      })
    }
    return `<div class="inline-image-ref" data-ref="${ref.index}">
      <div class="inline-ref-label">&#128444; 图片引用 [${ref.index}] · ${ref.chapter} · ${ref.page_label}</div>
      ${imgs || '<span>[图片加载失败]</span>'}
    </div>`
  })

  return DOMPurify.sanitize(html, {
    ADD_TAGS: ['table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'colgroup', 'col', 'caption',
               'span', 'div', 'img', 'p', 'sup', 'sub'],
    ADD_ATTR: ['class', 'data-ref', 'title', 'src', 'onerror', 'alt',
               'colspan', 'rowspan', 'align', 'valign', 'style'],
    FORCE_BODY: true,
  })
}

// ── 点击事件 ──────────────────────────────
function handleAnswerClick(event, msg) {
  const badge = event.target.closest('.cite-badge')
  if (!badge) return
  const refIdx = Number(badge.dataset.ref)
  const ref = (msg.references || []).find(r => r.index === refIdx)
  if (!ref) return

  activeRefIndex.value = refIdx
  activeRefMsgId.value = msg.id

  // 滚动到参考文献卡片
  const card = document.getElementById(`ref-${msg.id}-${refIdx}`)
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' })

  // 触发 PDF 高亮跳转
  emit('highlight', ref)
}

function handleRefClick(ref, msg) {
  activeRefIndex.value = ref.index
  activeRefMsgId.value = msg.id
  emit('highlight', ref)
  emit('jump-pdf', ref)
}

// ── 发送问题 ──────────────────────────────
async function sendQuery() {
  const q = inputText.value.trim()
  if (!q || loading.value) return
  inputText.value = ''

  const userMsg = { id: Date.now(), role: 'user', content: q }
  messages.value.push(userMsg)
  await scrollToBottom()

  loading.value = true
  const assistantId = Date.now() + 1

  if (streamMode.value) {
    await sendStream(q, assistantId)
  } else {
    await sendNonStream(q, assistantId)
  }

  loading.value = false
  await scrollToBottom()
}

async function sendNonStream(query, msgId) {
  try {
    const { data } = await axios.post('/api/query', {
      query,
      pdf_file_id: props.pdfFileId || null,
      stream: false,
    })
    messages.value.push({
      id: msgId, role: 'assistant',
      content: data.answer,
      references: data.references,
      streaming: false,
    })
  } catch (e) {
    messages.value.push({
      id: msgId, role: 'assistant',
      content: `请求失败：${e.message}`,
      references: [],
    })
  }
}

async function sendStream(query, msgId) {
  const assistantMsg = { id: msgId, role: 'assistant', content: '', references: [], streaming: true }
  messages.value.push(assistantMsg)

  try {
    const response = await fetch('/api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query,
        pdf_file_id: props.pdfFileId || null,
        stream: true,
      }),
    })

    const reader  = response.body.getReader()
    const decoder = new TextDecoder()
    let   buffer  = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      const lines = buffer.split('\n')
      buffer = lines.pop()

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6).trim()
        if (!raw) continue
        try {
          const evt = JSON.parse(raw)
          if (evt.type === 'meta') {
            assistantMsg.references = evt.references || []
          } else if (evt.type === 'token') {
            assistantMsg.content += evt.content
            await scrollToBottom()
          } else if (evt.type === 'done') {
            assistantMsg.streaming = false
            if (evt.answer != null && evt.answer !== '') {
              assistantMsg.content = evt.answer
            }
            if (Array.isArray(evt.references)) {
              assistantMsg.references = evt.references
            }
          }
        } catch (_) {}
      }
    }
  } catch (e) {
    assistantMsg.content  = `流式请求失败：${e.message}`
    assistantMsg.streaming = false
  }
}

async function scrollToBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

// 监听外部 send 事件
watch(() => props, () => {}, { deep: true })
defineExpose({ sendQuery: (q) => { inputText.value = q; sendQuery() } })
</script>

<style scoped>
.chat-panel { display: flex; flex-direction: column; height: 100%; }

.messages-area {
  flex: 1; overflow-y: auto; padding: 16px;
  display: flex; flex-direction: column; gap: 16px;
}

/* 空状态 */
.empty-state {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; height: 100%; gap: 12px; color: #9ca3af;
}
.empty-state p { font-size: 14px; }
.example-queries { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; max-width: 400px; }
.example-tag { cursor: pointer; }
.example-tag:hover { opacity: 0.8; }

/* 消息气泡 */
.message-item.user { display: flex; justify-content: flex-end; }
.user-bubble {
  display: flex; align-items: flex-start; gap: 8px;
  background: #4361ee; color: #fff; padding: 10px 14px;
  border-radius: 16px 4px 16px 16px; max-width: 70%; font-size: 14px;
}

.message-item.assistant { display: flex; }
.assistant-bubble { display: flex; gap: 10px; width: 100%; }
.assistant-avatar {
  width: 32px; height: 32px; border-radius: 8px; background: #eef2ff;
  display: flex; align-items: center; justify-content: center;
  color: #4361ee; flex-shrink: 0;
}
.assistant-body { flex: 1; min-width: 0; }

/* 流式加载点 */
.streaming-indicator {
  display: flex; gap: 5px; padding: 12px 8px; align-items: center;
}
.dot {
  width: 8px; height: 8px; border-radius: 50%; background: #4361ee;
  animation: bounce 1.2s infinite;
}
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce {
  0%, 80%, 100% { transform: translateY(0); opacity: 0.4; }
  40%            { transform: translateY(-6px); opacity: 1; }
}

/* 参考文献 */
.refs-section { margin-top: 14px; }
.refs-title {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; font-weight: 600; color: #6b7280; margin-bottom: 8px;
}
.ref-card {
  border: 1px solid #e4e7ed; border-radius: 8px; padding: 10px 12px;
  margin-bottom: 8px; cursor: pointer; transition: border-color 0.2s, box-shadow 0.2s;
  background: #fff;
}
.ref-card:hover { border-color: #4361ee; box-shadow: 0 2px 8px rgba(67,97,238,0.1); }
.ref-card.active { border-color: #4361ee; background: #f0f3ff; }
.ref-header {
  display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap;
}
.ref-index {
  background: #4361ee; color: #fff; border-radius: 50%;
  width: 20px; height: 20px; display: flex; align-items: center;
  justify-content: center; font-size: 11px; flex-shrink: 0;
}
.ref-chapter { font-size: 12px; color: #374151; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ref-page { font-size: 11px; color: #9ca3af; white-space: nowrap; }
.ref-preview { font-size: 12px; color: #6b7280; margin-top: 4px; }

/* 输入区 */
.input-area {
  flex-shrink: 0; padding: 12px 16px; border-top: 1px solid #e4e7ed;
  background: #fff;
}
.query-input-wrap { position: relative; }
.query-send-btn {
  position: absolute; right: 8px; bottom: 8px;
  padding: 6px 16px; font-size: 13px; z-index: 1;
}
.input-hint {
  display: flex; justify-content: space-between; align-items: center;
  margin-top: 6px; font-size: 12px; color: #9ca3af; padding: 0 2px;
}

/* 参考文献卡片内容区 */
.ref-preview-text { font-size: 12px; color: #9ca3af; }

/* 参考文献卡片 - 表格 */
.ref-table-wrap {
  overflow-x: auto;
  max-height: 260px;
  overflow-y: auto;
}
.ref-table-wrap :deep(table) {
  border-collapse: collapse; font-size: 11px; min-width: 100%;
}
.ref-table-wrap :deep(th),
.ref-table-wrap :deep(td) {
  border: 1px solid #d1d5db; padding: 3px 6px; white-space: nowrap;
}
.ref-table-wrap :deep(th) { background: #f3f4f6; font-weight: 600; }
.ref-table-wrap :deep(img),
.ref-rich-wrap :deep(img),
.ref-rich-wrap :deep(.rich-ref-root img) {
  max-width: 100%; height: auto; vertical-align: middle;
  margin: 6px 0; border-radius: 4px; display: inline-block;
}
.ref-rich-wrap :deep(.rich-ref-root) { font-size: 12px; color: #374151; line-height: 1.45; }

/* 参考文献卡片 - 图片 */
.ref-image-wrap { display: flex; flex-wrap: wrap; gap: 6px; }
.ref-img { max-width: 100%; max-height: 200px; border-radius: 4px; object-fit: contain; }

/* 内联表格/图片引用（回答正文中） */
.answer-content :deep(.inline-table-ref),
.answer-content :deep(.inline-image-ref) {
  border: 1px solid #e5e7eb; border-radius: 8px;
  padding: 10px 12px; margin: 12px 0; background: #fafbfc;
}
.answer-content :deep(.inline-ref-label) {
  font-size: 12px; color: #6b7280; margin-bottom: 8px; font-weight: 600;
}
.answer-content :deep(.inline-ref-content) {
  overflow-x: auto;
}
.answer-content :deep(.inline-ref-content table) {
  border-collapse: collapse; font-size: 12px; min-width: 100%;
}
.answer-content :deep(.inline-ref-content th),
.answer-content :deep(.inline-ref-content td) {
  border: 1px solid #d1d5db; padding: 4px 8px;
}
.answer-content :deep(.inline-ref-content th) { background: #f3f4f6; font-weight: 600; }
.answer-content :deep(.inline-ref-img) {
  max-width: 100%; max-height: 400px; border-radius: 6px; display: block; margin: 6px 0;
}
.answer-content :deep(.inline-ref-content img),
.answer-content :deep(.inline-ref-content .ref-inline-img) {
  max-width: 100%; max-height: 400px; border-radius: 6px; display: block; margin: 6px 0;
}
</style>

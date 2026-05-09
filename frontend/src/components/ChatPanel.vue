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
                  <div class="ref-preview" v-html="sanitize(ref.preview)" />
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
        <span>Ctrl+Enter 发送</span>
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
  table: 'warning',
  image: 'success',
  title: 'primary',
  text:  'info',
}

// ── 工具 ──────────────────────────────────
function sanitize(html) {
  return DOMPurify.sanitize(html || '', { ALLOWED_TAGS: ['b', 'strong', 'em', 'code', 'br', 'img'] })
}

function typeLabel(t) {
  return { table:'表格', image:'图片', title:'标题', text:'文本', list:'列表' }[t] || t
}

// 将回答文本中的引用编号 [1][2] 转换为可点击角标
function renderAnswer(content, references) {
  if (!content) return ''
  let html = marked.parse(content)

  // 把 [N] 转为可点击角标
  html = html.replace(/\[(\d+)\]/g, (_, n) => {
    const ref = (references || []).find(r => r.index === Number(n))
    const cls = (activeRefIndex.value === Number(n)) ? 'cite-badge active' : 'cite-badge'
    return `<span class="${cls}" data-ref="${n}" title="${ref ? ref.chapter : ''}">[${n}]</span>`
  })

  // 表格/图片 block 引用替换为内联展示
  if (references) {
    references.forEach(ref => {
      if (ref.block_type === 'table') {
        const placeholder = `> \\*\\*\\[表格引用\\]\\*\\* \`block_id=${ref.block_id}\`[^\\n]*`
        const tableHtml = `<div class="inline-table-ref" data-ref="${ref.index}">
          <div class="inline-ref-label">📊 表格引用 [${ref.index}] · ${ref.page_label}</div>
          <div class="inline-ref-content">${sanitize(ref.preview)}</div>
        </div>`
        html = html.replace(new RegExp(placeholder, 'g'), tableHtml)
      } else if (ref.block_type === 'image') {
        const placeholder = `> \\*\\*\\[图片引用\\]\\*\\* \`block_id=${ref.block_id}\`[^\\n]*`
        const imgHtml = `<div class="inline-image-ref" data-ref="${ref.index}">
          <div class="inline-ref-label">🖼 图片引用 [${ref.index}] · ${ref.page_label}</div>
          <img src="/images/${ref.block_id}" class="inline-ref-img"
               onerror="this.style.display='none'" />
        </div>`
        html = html.replace(new RegExp(placeholder, 'g'), imgHtml)
      }
    })
  }

  return DOMPurify.sanitize(html, {
    ADD_TAGS: ['span', 'div', 'img'],
    ADD_ATTR: ['class', 'data-ref', 'title', 'src', 'onerror'],
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

/* 内联表格/图片引用 */
.inline-table-ref, .inline-image-ref {
  border: 1px solid #e5e7eb; border-radius: 6px;
  padding: 8px 10px; margin: 8px 0; background: #fafbfc;
}
.inline-ref-label {
  font-size: 12px; color: #6b7280; margin-bottom: 6px; font-weight: 600;
}
.inline-ref-content { font-size: 12px; color: #374151; overflow-x: auto; }
.inline-ref-img { max-width: 100%; max-height: 300px; border-radius: 4px; }
</style>

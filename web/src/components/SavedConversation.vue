<script setup lang="ts">
import MarkdownView from './MarkdownView.vue'
import Icon from './Icon.vue'
import type { SavedTranscriptBlock } from '../api/types'

defineProps<{ history: { blocks: SavedTranscriptBlock[]; cursor: string | null; loading: boolean; error: string } }>()
defineEmits<{ more: []; retry: [] }>()
</script>

<template>
  <section class="saved-conversation" aria-label="保存的会话历史">
    <button v-if="history.cursor" class="btn" type="button" :disabled="history.loading" @click="$emit('more')">加载更早历史</button>
    <p v-if="history.loading" class="log-notice" role="status">正在加载保存的历史…</p>
    <div v-if="history.error" class="saved-history-error"><p role="alert">{{ history.error }}</p><button class="btn" type="button" :disabled="history.loading" @click="$emit('retry')">重试加载历史</button></div>
    <article v-for="block in history.blocks" :key="block.id" class="saved-message" :data-history-kind="block.kind">
      <template v-if="block.kind === 'user'">
        <div class="user-card">{{ block.content }}</div>
        <p v-for="filename in block.attachments" :key="filename" class="log-notice">附件：{{ filename }}</p>
      </template>
      <template v-else-if="block.kind === 'assistant'">
        <details v-if="block.thinking" class="saved-thinking"><summary>思考过程</summary><MarkdownView :text="block.thinking" /></details>
        <MarkdownView v-if="block.content" :text="block.content" />
      </template>
      <details v-else class="saved-tool" :class="{ 'is-error': block.is_error }">
        <summary><Icon name="terminal" :size="13" /><span>{{ block.name }}</span><span class="hint">{{ block.status === 'completed' ? '已完成' : block.status === 'error' ? '执行失败' : '未完成' }}</span></summary>
        <pre v-if="block.arguments" class="log-out">{{ block.arguments }}</pre>
        <pre v-if="block.content" class="log-out">{{ block.content }}</pre>
      </details>
      <p v-if="block.truncated" class="log-notice">历史内容较长，此条仅显示前 32,000 字符。</p>
    </article>
  </section>
</template>

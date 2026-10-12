<script setup lang="ts">
import type { RunView } from '../state/reducer';
import ToolTimeline from './ToolTimeline.vue';
defineProps<{
    view?: RunView;
    prompt?: string;
}>();
</script>
<template><div v-if="view?.snapshot" class="message-timeline"><div class="user-message">{{ prompt??'原始指令不可用（服务未持久化提交文本）' }}</div><div v-if="view.answer" class="answer">{{ view.answer }}</div><p v-if="view.snapshot.result.error" class="error" role="alert">{{ view.snapshot.result.error }}</p><slot></slot><details v-if="view.todos.length" class="detail-panel"><summary class="detail-toggle">任务计划</summary><pre>{{ JSON.stringify(view.todos,null,2) }}</pre></details><details v-if="Object.keys(view.usage).length" class="detail-panel"><summary class="detail-toggle">模型用量</summary><pre>{{ JSON.stringify(view.usage,null,2) }}</pre></details><ToolTimeline :tools="Object.values(view.tools)" /></div><div v-else class="empty muted"><p>选择或新建会话，向 Agent 发送指令。</p><p>执行日志与待处理交互会显示在这里。</p></div></template>

<script setup lang="ts">
import type { ToolView } from '../state/reducer';
import ToolDetailPanel from './ToolDetailPanel.vue';
defineProps<{
    tools: ToolView[];
}>();
</script>
<template><details v-if="tools.length" class="detail-panel"><summary class="detail-toggle"><strong>执行细节 · {{ tools.length }} 个工具调用<span v-if="tools.some(t=>t.status==='failed')" class="error"> · 工具失败</span></strong><span>⌄</span></summary><div class="detail-list"><ToolDetailPanel v-for="tool in tools" :key="tool.id" :tool="tool" /></div></details></template>

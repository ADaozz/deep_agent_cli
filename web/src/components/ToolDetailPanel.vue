<script setup lang="ts">
import TextWindow from './TextWindow.vue';
import type { ToolView } from '../state/reducer';
defineProps<{
    tool: ToolView;
}>();
</script>
<template><details class="tool-detail"><summary>{{ tool.name }} · {{ tool.status }}</summary><TextWindow :text="JSON.stringify(tool.arguments,null,2)" /><details v-if="tool.stdout" data-stream="stdout"><summary>stdout · {{ tool.stdout.length }} 字符</summary><TextWindow :text="tool.stdout" /></details><details v-if="tool.stderr" data-stream="stderr"><summary>stderr</summary><TextWindow :text="tool.stderr" /></details><details v-if="tool.result"><summary>工具结果</summary><TextWindow :text="tool.result" /></details></details></template>

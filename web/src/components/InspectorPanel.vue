<script setup lang="ts">
import type { RunView } from '../state/reducer';
import Icon from './Icon.vue';
import ToolDetailPanel from './ToolDetailPanel.vue';
import CodeChangesPanel from './CodeChangesPanel.vue';
defineProps<{
    view?: RunView;
    modal?: boolean;
}>();
defineEmits<{
    close: [
    ];
}>();
</script>
<template><aside :role="modal?'dialog':undefined" :aria-modal="modal?'true':undefined" class="inspector" aria-label="任务产出"><button class="drawer-close icon-button" aria-label="关闭任务产出" @click="$emit('close')">×</button><h2 class="inspector-title">任务产出</h2><details class="artifact-group"><summary class="artifact-trigger"><Icon name="diff" />代码变更</summary><CodeChangesPanel :key="view?.snapshot?.id" :tools="view?.tools??{}" /></details><details class="artifact-group"><summary class="artifact-trigger"><Icon name="terminal" />命令输出</summary><ToolDetailPanel v-for="tool in Object.values(view?.tools??{})" :key="tool.id" :tool="tool" /><p v-if="!Object.keys(view?.tools??{}).length" class="preview-note">暂无工具输出</p></details><details class="artifact-group"><summary class="artifact-trigger"><Icon name="file-check" />测试结果</summary><p class="preview-note">暂无结构化测试统计。验证证据请查看真实工具输出。</p></details><details class="artifact-group"><summary class="artifact-trigger"><Icon name="image" />预览 / 截图</summary><p class="preview-note">当前服务未提供二进制附件下载</p></details></aside></template>

<script setup lang="ts">
import { computed } from 'vue';
const props = defineProps<{
    status?: string;
}>();
const labels: Record<string, string> = { running: '执行中', waiting_confirmation: '等待审批', waiting_human: '等待回答', paused: '已暂停', runtime_config_boundary: '等待继续', cancelling: '取消中', completed: '已完成', failed: '执行失败', cancelled: '已取消', interrupted: '服务重启中断' };
const label = computed(() => props.status ? labels[props.status] ?? props.status : '等待指令');
</script>
<template><span class="run-status" :data-status="status" role="status">{{ label }}</span></template>

<script setup lang="ts">
import RunStatus from './RunStatus.vue';
defineProps<{
    session: string;
    status?: string;
}>();
</script>
<template><div class="thread-top"><div><h1>{{ session?'会话 '+session.slice(0,8):'项目工作区' }}</h1><div class="meta"><RunStatus :status="status" /></div></div><slot></slot></div></template>

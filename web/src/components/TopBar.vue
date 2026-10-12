<script setup lang="ts">
import Icon from './Icon.vue';
defineProps<{
    sidebar: boolean;
    inspector: boolean;
}>();
defineEmits<{
    sidebar: [
    ];
    inspector: [
    ];
    search: [
    ];
}>();
</script>
<template><header class="topbar"><div class="top-left"><button class="icon-button" aria-label="项目导航" :aria-expanded="sidebar" @click="$emit('sidebar')"><Icon name="panel" /></button><span class="brand">Deep Agent</span><span class="separator"></span><span class="breadcrumb">/ Project Workspace</span></div><div class="top-right"><button class="icon-button" aria-label="筛选会话" @click="$emit('search')"><Icon name="search" /></button><button class="icon-button" aria-label="任务产出" :aria-expanded="inspector" @click="$emit('inspector')"><Icon name="diff" /></button></div></header></template>

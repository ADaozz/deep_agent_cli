<script setup lang="ts">
import { ref, watch } from 'vue';
import type { Project, Session } from '../api/types';
import Icon from './Icon.vue';
import SessionList from './SessionList.vue';
const props = defineProps<{
    projects: Project[];
    sessions: Session[];
    project: string;
    session: string;
    busy: boolean;
    search: boolean;
    health: boolean;
    modal?: boolean;
}>();
defineEmits<{
    project: [
        id: string
    ];
    session: [
        session: Session
    ];
    create: [
    ];
    close: [
    ];
}>();
const filter = ref('');
const input = ref<HTMLInputElement | null>(null);
watch(() => props.search, () => { setTimeout(() => input.value?.focus(), 0); });
</script>
<template><aside :role="modal?'dialog':undefined" :aria-modal="modal?'true':undefined" class="sidebar" aria-label="项目与会话"><button class="drawer-close icon-button" aria-label="关闭项目导航" @click="$emit('close')">×</button><p class="section-label">PROJECTS</p><input v-if="search" ref="input" v-model="filter" class="search-input" aria-label="筛选最近会话 ID" placeholder="筛选最近会话 ID"><p v-if="!projects.length" class="empty muted">暂无配置项目</p><section v-for="p in projects" :key="p.id"><div class="project-title"><button class="project-select" :aria-pressed="p.id===project" @click="$emit('project',p.id)"><Icon name="folder" /><span class="project-name">{{ p.id }}</span></button><button v-if="p.id===project" class="icon-button" aria-label="新建会话" :disabled="busy" @click="$emit('create')">+</button></div><SessionList v-if="p.id===project" :sessions="sessions" :selected="session" :filter="filter" @select="$emit('session',$event)" /><div class="sidebar-divider"></div></section><div class="sidebar-foot"><span v-if="health" class="ready-dot"></span>本地单用户工作区</div></aside></template>

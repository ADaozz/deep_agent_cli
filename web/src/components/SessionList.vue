<script setup lang="ts">
import type { Session } from '../api/types';
defineProps<{
    sessions: Session[];
    selected: string;
    filter: string;
}>();
defineEmits<{
    select: [
        session: Session
    ];
}>();
</script>
<template><nav class="session-group" aria-label="项目会话"><button v-for="session in sessions.filter(s=>s.id.includes(filter))" :key="session.id" class="session" :class="{active:session.id===selected}" :aria-current="session.id===selected?'page':undefined" @click="$emit('select',session)"><span class="session-title">会话 {{ session.id.slice(0,8) }}</span><span class="session-state">{{ new Date(session.created_at).toLocaleString() }}</span></button><p v-if="sessions.length&&!sessions.some(s=>s.id.includes(filter))" class="empty muted" role="status">没有匹配的会话</p><p v-if="!sessions.length" class="empty muted">暂无会话，点击 + 新建</p></nav></template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import Icon from './Icon.vue';
const props = defineProps<{
    disabled: boolean;
    active: boolean;
    runId?: string;
}>();
const emit = defineEmits<{
    send: [
        text: string
    ];
}>();
const text = ref('');
const composing = ref(false);
watch(() => props.runId, () => { text.value = ''; });
function submit() { if (!props.disabled && text.value.trim()) {
    emit('send', text.value);
} }
function keydown(event: KeyboardEvent) { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !composing.value) {
    event.preventDefault();
    if (!(event.target as HTMLTextAreaElement).disabled)
        submit();
} }
</script>
<template><div class="input-zone"><form class="composer" @submit.prevent="submit"><textarea v-model="text" aria-label="向 Agent 输入指令" placeholder="向 Agent 输入指令..." :disabled="disabled" @keydown="keydown" @compositionstart="composing=true" @compositionend="composing=false"></textarea><div class="composer-actions"><span class="composer-hint">{{ active?'当前任务未结束；服务不支持队列或追加指令':'Enter 发送 · Shift+Enter 换行' }}</span><button class="send-btn" type="submit" aria-label="发送指令" :disabled="disabled||!text.trim()"><Icon name="send" /></button></div></form></div></template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import type { Run } from '../api/types';
import { terminal } from '../state/reducer';
const props = defineProps<{
    run?: Run | null;
    busy: boolean;
}>();
defineEmits<{
    cancel: [
    ];
    continue: [
    ];
    resume: [
    ];
}>();
const active = computed(() => props.run && !terminal.has(props.run.status));
const confirmed = ref(false);
</script>
<template><div class="run-actions"><button v-if="active" class="quiet-button" :disabled="busy||run?.status==='cancelling'" @click="$emit('cancel')">{{ run?.status==='cancelling'?'取消中':'取消任务' }}</button><button v-if="run&&['paused','runtime_config_boundary'].includes(run.status)" class="quiet-button" :disabled="busy" @click="$emit('continue')">继续运行</button><div v-if="run&&['interrupted','cancelled','failed'].includes(run.status)" class="resume-confirm"><label><input v-model="confirmed" type="checkbox">我已检查副作用，确认恢复会话</label><button class="quiet-button" :disabled="busy||!confirmed" @click="$emit('resume')">恢复会话</button></div></div></template>

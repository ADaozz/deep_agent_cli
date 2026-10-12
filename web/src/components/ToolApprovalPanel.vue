<script setup lang="ts">
import TextWindow from './TextWindow.vue';
import { ref, watch, computed } from 'vue';
import type { PendingToolCall, ApprovalBody } from '../api/types';
import { dictionary } from '../state/dictionary';
import Icon from './Icon.vue';
import { approvalBody } from '../state/forms';
const props = defineProps<{
    calls: PendingToolCall[];
    interruptId: string;
    project: string;
    busy: boolean;
}>();
const emit = defineEmits<{
    submit: [
        body: ApprovalBody
    ];
}>();
const choices = ref(dictionary<'approve' | 'reject'>());
watch(() => props.interruptId, () => { choices.value = dictionary<'approve' | 'reject'>(); });
const complete = computed(() => props.calls.length > 0 && props.calls.every(c => ['approve', 'reject'].includes(choices.value[c.toolCallId] ?? '')));
function submit() { if (complete.value && !props.busy)
    emit('submit', { interrupt_id: props.interruptId, decisions: approvalBody(props.calls, choices.value) }); }
function summary(call: PendingToolCall) { return String(call.args.command ?? call.args.file_path ?? call.args.path ?? call.name); }
</script>
<template><section class="approval-card" aria-label="工具审批"><h2><Icon name="shield" />Agent 请求执行{{ calls.length===1&&calls[0]?.name==='execute'?'命令':'工具' }}</h2><div v-for="call in calls" :key="call.toolCallId" class="approval-tool"><div class="approval-scope">工作区 · {{ project }} · {{ call.name }}</div><div class="approval-command"><div class="dim">{{ call.name }}</div><TextWindow :text="summary(call)" /></div><p v-if="call.name==='execute'" class="approval-network">{{ call.args.network===true?'Network: ON · 允许访问宿主网络':call.args.network===false?'Network: OFF · 直接 IP 网络隔离':'网络权限未提供' }}</p><details class="tool-args"><summary>完整工具参数</summary><TextWindow :text="JSON.stringify(call.args,null,2)" /></details><div class="approval-decisions" :aria-label="'工具 '+call.toolCallId+' 的决定'"><button class="decision reject" :disabled="busy" :aria-pressed="choices[call.toolCallId]==='reject'" @click="choices[call.toolCallId]='reject'">拒绝 {{ call.name }}</button><button class="decision approve" :disabled="busy" :aria-pressed="choices[call.toolCallId]==='approve'" @click="choices[call.toolCallId]='approve'">允许执行 {{ call.name }}</button></div></div><p class="muted">只有提交整批决定后，服务才会继续。未选项不会默认批准或拒绝。</p><div class="approval-footer"><button class="decision approve" :disabled="!complete||busy" @click="submit">提交决定</button></div></section></template>

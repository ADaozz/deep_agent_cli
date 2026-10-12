<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, watch, computed } from 'vue';
import { useWorkspace } from '../state/workspace';
import { api } from '../api/http';
import type { ApprovalBody, HumanInputBody, HumanInputRequest } from '../api/types';
import ToolApprovalPanel from './ToolApprovalPanel.vue';
import HumanInputForm from './HumanInputForm.vue';
import SvgSymbols from './SvgSymbols.vue';
import TopBar from './TopBar.vue';
import ProjectSidebar from './ProjectSidebar.vue';
import ThreadHeader from './ThreadHeader.vue';
import MessageTimeline from './MessageTimeline.vue';
import ChatComposer from './ChatComposer.vue';
import RunActionBar from './RunActionBar.vue';
import InspectorPanel from './InspectorPanel.vue';
const workspace = useWorkspace();
const { state, view, run, occupied } = workspace;
const sidebar = ref(false), inspector = ref(false), search = ref(false);
const width = ref(window.innerWidth);
const modalSidebar = computed(() => width.value <= 560 && sidebar.value);
const modalInspector = computed(() => width.value <= 820 && inspector.value);
const modal = computed(() => modalSidebar.value || modalInspector.value);
function resized() { width.value = window.innerWidth; }
const thread = ref<HTMLElement | null>(null);
let stick = true;
let lastFocus: HTMLElement | null = null;
function approvals(body: ApprovalBody) { void workspace.act(current => api.approvals(current.id, body)); }
function human(body: HumanInputBody) { void workspace.act(current => api.human(current.id, body)); }
function searchSessions() { search.value = !search.value; if (search.value) {
    lastFocus = document.activeElement as HTMLElement;
    sidebar.value = width.value <= 560;
} }
function cancel() { void workspace.act(current => api.cancel(current.id)); }
function continueRun() { void workspace.act(current => api.continue(current.id, current.interrupt_id!)); }
function drawer(which: 'sidebar' | 'inspector') { lastFocus = document.activeElement as HTMLElement; if (which === 'sidebar')
    sidebar.value = !sidebar.value;
else
    inspector.value = !inspector.value; if ((which === 'sidebar' && width.value <= 560) || (which === 'inspector' && width.value <= 820))
    void nextTick(() => document.querySelector<HTMLElement>(`.${which} button`)?.focus()); }
function close() { sidebar.value = false; inspector.value = false; lastFocus?.focus(); }
function keydown(e: KeyboardEvent) { if (e.key === 'Escape')
    close(); if (e.key === 'Tab' && modal.value) {
    const panel = document.querySelector(modalSidebar.value ? '.sidebar' : '.inspector');
    const fields = Array.from(panel?.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary') ?? []).filter(el => el.getClientRects().length);
    const first = fields[0], last = fields.at(-1);
    if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last?.focus();
    }
    else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first?.focus();
    }
} }
function scrolled() { const el = thread.value; if (el)
    stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }
watch(() => view.value?.answer, () => { if (stick)
    void nextTick(() => { const el = thread.value; if (el)
        el.scrollTop = el.scrollHeight; }); });
onMounted(() => { void workspace.init(); document.addEventListener('keydown', keydown); window.addEventListener('resize', resized); });
onUnmounted(() => { workspace.stop(); document.removeEventListener('keydown', keydown); window.removeEventListener('resize', resized); });
</script>
<template><div class="app"><SvgSymbols /><TopBar :sidebar="width<=560?sidebar:!sidebar" :inspector="width<=820?inspector:!inspector" @sidebar="drawer('sidebar')" @inspector="drawer('inspector')" @search="searchSessions" /><div class="workspace" :class="{'sidebar-open':sidebar,'inspector-open':inspector}"><button v-if="sidebar||inspector" class="drawer-backdrop" aria-label="关闭面板" @click="close"></button><ProjectSidebar :modal="modalSidebar" :inert="modalInspector" :projects="state.projects" :sessions="state.sessions" :project="state.project" :session="state.session" :busy="state.busy" :search="search" :health="state.health" @project="workspace.selectProject($event)" @session="workspace.selectSession($event);close()" @create="workspace.createSession()" @close="close" /><main class="main" :inert="modal"><ThreadHeader :session="state.session" :status="run?.status"><RunActionBar :run="run" :busy="state.busy" @cancel="cancel" @continue="continueRun" @resume="workspace.resume()" /></ThreadHeader><div ref="thread" class="thread-body" @scroll="scrolled"><div class="thread-content"><div v-if="state.history.length" class="run-history"><label>任务历史<select aria-label="选择任务" :value="state.runId" @change="workspace.selectRun(($event.target as HTMLSelectElement).value)"><option v-for="item in state.history" :key="item.id" :value="item.id">{{ item.id.slice(0,8) }} · {{ item.status }} · {{ new Date(item.created_at).toLocaleString() }}</option></select></label><button v-if="state.nextCursor" class="quiet-button" @click="workspace.moreHistory()">加载更多任务</button></div><p v-if="state.projectActive&&state.projectActive.id!==state.runId" class="connection-notice">项目已有活动任务 <button class="quiet-button" @click="workspace.openActive()">查看活动任务</button></p><p v-if="state.blocked" role="alert" class="error">项目清理失败，服务仍保留占用，请重启服务</p><p v-if="state.error" class="error" role="alert">{{ state.error }}</p><div v-if="view?.diagnostic" class="connection-notice" role="status">{{ view.diagnostic }} <button class="quiet-button" @click="workspace.reconnect()">重新连接</button></div><MessageTimeline :view="view" :prompt="state.prompts[state.runId]"><ToolApprovalPanel v-if="run?.status==='waiting_confirmation'&&run.interrupt_id" :key="run.interrupt_id+state.interactionRevision" :calls="run.result.pending_tool_calls??[]" :interrupt-id="run.interrupt_id" :project="run.project_id" :busy="state.busy" @submit="approvals" /><HumanInputForm v-if="run?.status==='waiting_human'&&run.interrupt_id" :key="run.interrupt_id+state.interactionRevision" :request="run.result.human_input as HumanInputRequest" :interrupt-id="run.interrupt_id" :busy="state.busy" @submit="human" /></MessageTimeline><button v-if="state.error&&!run" class="quiet-button" @click="workspace.reconnect()">重新连接服务</button></div></div><ChatComposer :run-id="state.runId" :disabled="state.busy||occupied||!state.project" :active="occupied" @send="workspace.send($event)" /></main><InspectorPanel :modal="modalInspector" :inert="modalSidebar" :view="view" @close="close" /></div></div></template>

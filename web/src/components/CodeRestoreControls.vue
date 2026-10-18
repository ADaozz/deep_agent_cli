<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue';
import { api, ApiError } from '../api/client';
import type { CodeCheckpoint, Run } from '../api/types';
import { isTerminal } from '../api/types';
import { apiConfig } from '../store/settings';

const props = defineProps<{ run: Run; busy: boolean; projectBusy: boolean }>();
const emit = defineEmits<{ busy: [value: boolean]; changed: [] }>();
const checkpoint = ref<CodeCheckpoint>();
const error = ref('');
const loading = ref(false);
const pending = ref(false);
const confirmation = ref<'undo' | 'redo' | null>(null);
const visibleFiles = ref(20);
let generation = 0;

async function load() {
    const version = ++generation;
    const id = props.run.id;
    confirmation.value = null;
    checkpoint.value = undefined;
    error.value = '';
    visibleFiles.value = 20;
    loading.value = true;
    try {
        const data = await api.codeCheckpoint(apiConfig(), id);
        if (version === generation && data.run_id === id)
            checkpoint.value = data;
    } catch (e) {
        if (version === generation)
            error.value = e instanceof Error ? e.message : '无法读取代码快照';
    } finally {
        if (version === generation)
            loading.value = false;
    }
}
watch([() => props.run.id, () => props.run.status, () => props.projectBusy], () => { void load(); }, { immediate: true });
onUnmounted(() => { generation++; });

async function restore() {
    const action = confirmation.value, current = checkpoint.value;
    if (!action || !current?.revision || props.busy || props.projectBusy || pending.value || !isTerminal(props.run.status))
        return;
    if (action === 'undo' ? !current.can_undo : !current.can_redo)
        return;
    const version = generation, id = props.run.id;
    pending.value = true;
    emit('busy', true);
    error.value = '';
    try {
        const data = await api.restoreCode(apiConfig(), id, action, current.revision);
        if (version === generation && data.run_id === id) {
            checkpoint.value = data;
            confirmation.value = null;
        }
    } catch (e) {
        if (version === generation) {
            error.value = e instanceof ApiError ? `${e.status} · ${e.message}` : e instanceof Error ? e.message : '代码回退失败';
            confirmation.value = null;
            // Reconcile a stale revision; never retry a write automatically.
            try {
                const data = await api.codeCheckpoint(apiConfig(), id);
                if (version === generation && data.run_id === id)
                    checkpoint.value = data;
            } catch { /* Retain the original failure; the user may recheck. */ }
        }
    } finally {
        pending.value = false;
        emit('busy', false);
        emit('changed');
    }
}
</script>

<template>
  <section class="code-restore" aria-label="轮次代码回退">
    <p v-if="loading" class="help">正在检查本轮代码快照…</p>
    <template v-if="checkpoint">
      <p v-if="checkpoint.status==='reverted'" class="code-restore-status" role="status">本轮代码已回退</p>
      <p v-if="!checkpoint.available" class="help">{{ checkpoint.reason || '本轮快照仍在记录，暂不能回退' }}</p>
      <template v-else>
        <details v-if="checkpoint.files.length" class="restore-files">
          <summary>本轮快照 · {{ checkpoint.files.length }} 个路径</summary>
          <ul><li v-for="file in checkpoint.files.slice(0,visibleFiles)" :key="file.path">{{ file.path }} · {{ file.change==='create'?'新建':file.change==='delete'?'删除':'修改' }}</li></ul>
          <button v-if="visibleFiles<checkpoint.files.length" class="btn" :disabled="pending" @click="visibleFiles+=20">查看更多路径</button>
        </details>
        <p v-else class="help">本轮没有需要回退的最终文件差异。</p>
        <p v-if="checkpoint.files.length" class="help">按本轮工作区快照回退，包含运行期间同时发生的修改；只改代码，不撤销对话。后续修改会触发冲突。</p>
        <p v-if="projectBusy" class="help">请先完成或取消项目中的活动任务。</p>
        <div v-if="confirmation" class="restore-confirm">
          <p>{{ confirmation==='undo'?'恢复到本轮开始前的代码？':'恢复本轮结束时的代码？' }}</p>
          <p class="help">将修改上方快照列表中的路径。Agent 仍保留原对话历史。</p>
          <button class="btn" :disabled="busy||pending||projectBusy" @click="restore">{{ confirmation==='undo'?'确认回退本轮':'确认撤销回退' }}</button>
          <button class="btn" :disabled="pending" @click="confirmation=null">取消</button>
        </div>
        <button v-else-if="checkpoint.can_undo||checkpoint.can_redo" class="btn" :disabled="busy||pending||projectBusy||!isTerminal(run.status)" @click="confirmation=checkpoint.can_redo?'redo':'undo'">{{ checkpoint.can_redo?'撤销回退':'回退本轮代码' }}</button>
      </template>
    </template>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <button v-if="error" class="btn" :disabled="busy||pending" @click="load">重新检查快照</button>
  </section>
</template>

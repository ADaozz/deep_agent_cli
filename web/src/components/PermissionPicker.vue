<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api/client'
import type { Capabilities } from '../api/types'
import { apiConfig } from '../store/settings'
import { permissionDrafts } from '../store/capabilities'
import { models } from '../store/models'
import Popover from './Popover.vue'
import Icon from './Icon.vue'
const props = defineProps<{ projectId: string; sessionId: string; disabled: boolean }>()
const open = ref(false)
const catalog = ref<Capabilities>()
const mode = ref<'ask' | 'allow'>('ask')
const error = ref('')
const busy = ref(false)
const confirmOpen = ref(false)
const confirmation = ref('')
let generation = 0
const blocked = computed(() => props.disabled || busy.value || !!models.changing[props.sessionId])
watch(() => [props.projectId, props.sessionId], async () => {
  const version = ++generation
  open.value = false; confirmOpen.value = false; error.value = ''; catalog.value = undefined
  if (!props.projectId) return
  try {
    const [result, permission] = await Promise.all([
      api.capabilities(apiConfig(), props.projectId),
      props.sessionId ? api.permission(apiConfig(), props.sessionId) : Promise.resolve(permissionDrafts[props.projectId] || { mode: 'ask' as const }),
    ])
    if (version === generation) { catalog.value = result; mode.value = permission.mode }
  } catch (e) { if (version === generation) error.value = e instanceof Error ? e.message : '无法加载权限' }
}, { immediate: true })
async function selectPermission(value: 'ask' | 'allow'): Promise<void> {
  if (blocked.value) return
  const version = generation
  const session = props.sessionId
  busy.value = true; error.value = ''
  if (session) models.changing[session] = true
  try {
    if (session) await api.selectPermission(apiConfig(), session, value, confirmation.value)
    else permissionDrafts[props.projectId] = { mode: value, confirmation: confirmation.value }
    if (version === generation) { mode.value = value; open.value = false; confirmOpen.value = false; confirmation.value = '' }
  } catch (e) { if (version === generation) error.value = e instanceof Error ? e.message : '无法切换权限' }
  finally { busy.value = false; if (session) models.changing[session] = false }
}
</script>
<template>
  <span class="anchor">
    <button class="model-pick" type="button" aria-label="切换权限" :disabled="blocked" @click="open = !open">
      {{ mode }}<Icon name="chevronDown" :size="10" />
    </button>
    <Popover v-if="open" align="up" end @close="open = false">
      <div class="pop-head">权限</div>
      <button class="pop-item" type="button" :disabled="blocked || !catalog" @click="selectPermission('ask')"><span class="grow">ask · 逐次审批</span><Icon v-if="mode === 'ask'" name="check" :size="12" /></button>
      <button class="pop-item" type="button" :disabled="blocked || !catalog?.allow_available" @click="confirmOpen = true; confirmation = ''"><span class="grow">allow · 自动批准</span><Icon v-if="mode === 'allow'" name="check" :size="12" /></button>
      <p v-if="catalog && !catalog.allow_available" class="pop-note">allow 仅在沙箱模式下可用</p>
      <p v-if="error" class="pop-note model-error" role="alert">{{ error }}</p>
    </Popover>
    <Teleport to="body">
      <div v-if="confirmOpen" class="scrim" @click.self="!busy && (confirmOpen = false)">
        <form class="dialog" role="dialog" aria-modal="true" aria-labelledby="allow-title" @submit.prevent="selectPermission('allow')">
          <header class="dialog-head"><h2 id="allow-title">启用 ALLOW（高风险）</h2></header>
          <div class="dialog-body">
            <p>{{ catalog?.allow_warning }}</p>
            <p>自动批准所有工具（包括写入和删除），并开放沙箱网络。切回 ask 只影响后续命令。</p>
            <label for="allow-confirm">输入 ALLOW 确认</label><input id="allow-confirm" v-model="confirmation" class="attachment-path-input" autocomplete="off" :disabled="busy" />
            <p v-if="error" role="alert">{{ error }}</p>
          </div>
          <footer class="dialog-foot"><button class="btn" type="button" :disabled="busy" @click="confirmOpen = false">取消</button><button class="btn is-primary" type="submit" :disabled="confirmation !== 'ALLOW' || blocked">启用 ALLOW</button></footer>
        </form>
      </div>
    </Teleport>
  </span>
</template>

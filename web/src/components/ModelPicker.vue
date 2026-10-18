<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Icon from './Icon.vue'
import Popover from './Popover.vue'
import { loadModels, models, selectModel } from '../store/models'
import { ApiError } from '../api/client'

const props = defineProps<{ projectId: string; sessionId?: string; disabled: boolean; end?: boolean }>()
const open = ref(false)
const step = ref<'source' | 'model' | 'effort'>('source')
const source = ref('')
const modelId = ref('')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
let generation = 0
const catalog = computed(() => models.catalogs[props.projectId])
const current = computed(() => props.sessionId ? models.sessions[props.sessionId] : models.drafts[props.projectId] || catalog.value?.current)
const sources = computed(() => [...new Set(catalog.value?.models.map(model => model.source) || [])])
const choices = computed(() => catalog.value?.models.filter(model => model.source === source.value) || [])
const profile = computed(() => catalog.value?.models.find(model => model.id === modelId.value))

async function load(): Promise<void> {
  const version = ++generation
  if (!props.projectId) return
  loading.value = true
  error.value = ''
  try { await loadModels(props.projectId, props.sessionId) }
  catch (e) { if (version === generation) error.value = e instanceof Error ? e.message : '无法加载模型' }
  finally { if (version === generation) loading.value = false }
}
watch(() => [props.projectId, props.sessionId], () => { open.value = false; void load() }, { immediate: true })
watch(() => props.disabled, disabled => { if (disabled) open.value = false })

function toggle(): void {
  if (props.disabled || saving.value || !props.projectId) return
  open.value = !open.value
  if (open.value) { step.value = 'source'; source.value = ''; modelId.value = ''; void load() }
}
function back(): void { step.value = step.value === 'effort' ? 'model' : 'source'; error.value = '' }
async function choose(effort: string): Promise<void> {
  if (!profile.value || saving.value || props.disabled) return
  saving.value = true
  error.value = ''
  try {
    await selectModel(props.projectId, props.sessionId || '', modelId.value, effort)
    open.value = false
  } catch (e) {
    error.value = e instanceof ApiError ? `${e.status} · ${e.message}` : e instanceof Error ? e.message : '模型切换失败'
  } finally { saving.value = false }
}
</script>

<template>
  <span class="anchor model-selector">
    <button class="model-pick" type="button" aria-label="选择模型" :aria-expanded="open" :disabled="disabled || saving || !projectId" :title="disabled ? '请先结束当前运行或交互' : current?.model || '选择模型'" @click="toggle">
      {{ current?.model || '选择模型' }}
      <Icon name="chevronDown" :size="11" />
    </button>
    <Popover v-if="open" align="up" :end="end" @close="open = false">
      <div class="pop-head model-step">
        <button v-if="step !== 'source'" class="ico" type="button" aria-label="上一步" :disabled="saving" @click="back"><Icon name="chevronLeft" :size="12" /></button>
        {{ step === 'source' ? '选择组织' : step === 'model' ? '选择模型名称' : '选择思考强度' }}
      </div>
      <p v-if="loading" class="pop-note">正在加载模型…</p>
      <template v-else-if="step === 'source'">
        <button v-for="item in sources" :key="item" class="pop-item" type="button" @click="source = item; step = 'model'">
          <span class="grow">{{ item }}</span><Icon v-if="current?.source === item" name="check" :size="13" />
        </button>
        <p v-if="!sources.length && !error" class="pop-note">尚未配置可用模型</p>
      </template>
      <template v-else-if="step === 'model'">
        <button v-for="item in choices" :key="item.id" class="pop-item" type="button" @click="modelId = item.id; step = 'effort'">
          <span class="grow">{{ item.model }}</span><Icon v-if="current?.model_id === item.id" name="check" :size="13" />
        </button>
      </template>
      <template v-else>
        <button v-for="effort in profile?.reasoning_efforts || []" :key="effort" class="pop-item" type="button" :disabled="saving" @click="choose(effort)">
          <span class="grow">{{ effort === 'default' ? '默认' : effort }}</span><Icon v-if="current?.model_id === modelId && current?.reasoning_effort === effort" name="check" :size="13" />
        </button>
        <p v-if="saving" class="pop-note">正在切换…</p>
      </template>
      <p v-if="error" class="pop-note model-error" role="alert">{{ error }}</p>
      <button v-if="error && step === 'source'" class="pop-item" type="button" @click="load">重新加载</button>
    </Popover>
  </span>
</template>

<script setup lang="ts">
/**
 * Local display preferences and gateway connection status.
 */
import { onBeforeUnmount, reactive, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { ApiError, NetworkError, api, describeDetail, getHealth } from '../api/client'
import {
  DEFAULT_MODEL_LABEL,
  applySettings,
  settings,
} from '../store/settings'

const props = defineProps<{ open: boolean; projects: { id: string }[] }>()
const emit = defineEmits<{ close: []; saved: []; 'clear-records': [] }>()

const draft = reactive({
  baseUrl: settings.baseUrl,
  modelLabel: settings.modelLabel,
  labels: { ...settings.projectLabels },
})
const testing = ref(false)
const testResult = reactive({ ok: false, text: '' })
const confirmingClear = ref(false)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    draft.baseUrl = settings.baseUrl
    draft.modelLabel = settings.modelLabel
    draft.labels = { ...settings.projectLabels }
    testResult.ok = false
    testResult.text = ''
    confirmingClear.value = false
  },
)

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close')
}

watch(
  () => props.open,
  (open) => {
    if (open) window.addEventListener('keydown', onKey)
    else window.removeEventListener('keydown', onKey)
  },
)

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

async function test(): Promise<void> {
  testing.value = true
  testResult.ok = false
  testResult.text = ''
  try {
    const health = await getHealth(draft.baseUrl)
    if (!health) {
      testResult.text = '健康检查失败：/health 未返回 {"status":"ok"}。'
      return
    }
    const result = await api.projects({ baseUrl: draft.baseUrl })
    const ids = (result.projects ?? []).map((project) => project.id)
    testResult.ok = true
    testResult.text = ids.length
      ? `已连接 — 仓库：${ids.join('、')}`
      : '已连接，但服务配置里没有仓库。'
  } catch (error) {
    if (error instanceof NetworkError) {
      testResult.text =
        '暂时无法连接，请检查本地服务和网关是否已启动。'
    } else if (error instanceof ApiError) {
      testResult.text = `HTTP ${error.status}：${describeDetail(error.detail) || '鉴权或请求失败'}`
    } else {
      testResult.text = error instanceof Error ? error.message : String(error)
    }
  } finally {
    testing.value = false
  }
}

function save(): void {
  const labels: Record<string, string> = {}
  for (const [key, value] of Object.entries(draft.labels)) {
    if (value && value.trim()) labels[key] = value.trim()
  }
  applySettings({
    baseUrl: draft.baseUrl,
    projectLabels: labels,
    modelLabel: draft.modelLabel.trim() || DEFAULT_MODEL_LABEL,
  })
  emit('saved')
  emit('close')
}

function clearRecords(): void {
  if (!confirmingClear.value) {
    confirmingClear.value = true
    return
  }
  emit('clear-records')
  confirmingClear.value = false
}
</script>

<template>
  <div v-if="open" class="scrim" @click.self="emit('close')">
    <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <header class="dialog-head">
        <h2 id="settings-title">连接设置</h2>
        <button class="ico" type="button" aria-label="关闭" @click="emit('close')">
          <Icon name="close" :size="14" />
        </button>
      </header>

      <div class="dialog-body">
        <p class="help">通过本地网关连接 Deep Agent。可测试连接并调整本机显示偏好。</p>

        <div class="field">
          <label for="model-label">模型显示名</label>
          <input id="model-label" v-model="draft.modelLabel" type="text" :placeholder="DEFAULT_MODEL_LABEL" />
          <p class="help">
            该名称只用于 composer 上的显示。模型本身由服务配置固定；deep-agent 没有模型选择接口。
          </p>
        </div>

        <div v-if="projects.length" class="field">
          <span>仓库显示名（仅本地）</span>
          <div v-for="project in projects" :key="project.id" class="field">
            <label :for="`label-${project.id}`" class="help">{{ project.id }}</label>
            <input :id="`label-${project.id}`" v-model="draft.labels[project.id]" type="text" :placeholder="project.id" />
          </div>
        </div>

        <div class="field">
          <div class="field-row" style="align-items: center">
            <button class="btn" type="button" :disabled="testing" @click="test">
              {{ testing ? '测试中…' : '测试连接' }}
            </button>
            <button class="btn" type="button" @click="clearRecords">
              {{ confirmingClear ? '确认清除' : '清除本地数据' }}
            </button>
          </div>
          <p v-if="testResult.text" class="help" :style="testResult.ok ? { color: 'var(--green)' } : { color: 'var(--danger)' }">
            {{ testResult.text }}
          </p>
          <p class="help">
            本机保存提问和草稿；清除后仍可从服务恢复运行记录与日志，但无法恢复原始提问。
          </p>
        </div>
      </div>

      <footer class="dialog-foot">
        <span class="grow">权限模式固定为询问；仓库列表来自服务配置。</span>
        <button class="btn" type="button" @click="emit('close')">取消</button>
        <button class="btn is-primary" type="button" @click="save">保存并重连</button>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * Approvals and human-input questions, rendered as flat inline rows inside the
 * work log (no cards, no borders) — the minimal style of the real page.
 *
 * The pending interaction always comes from the authoritative Run snapshot, and
 * every pending tool call must be covered exactly once, so multi-tool batches
 * accumulate local decisions and then send them in one request.
 */
import { computed, reactive, watch } from 'vue'
import type { ApprovalDecision, InputField } from '../api/types'
import Icon from './Icon.vue'
import type { InteractionModel } from '../lib/runView'
import { summarizeCall } from '../lib/timeline'

const props = defineProps<{
  interaction: InteractionModel
  submitted: boolean
  rejected: string
  busy: boolean
}>()

const emit = defineEmits<{
  approve: [ApprovalDecision[]]
  human: [Record<string, unknown>]
  continue: []
}>()

/* ------------------------------------------------------------------ approvals */

const decisions = reactive<Record<string, 'approve' | 'reject'>>({})
const pending = computed(() => props.interaction.pendingTools)
const locked = computed(() => props.submitted || props.busy || !props.interaction.interruptId)
const allDecided = computed(
  () => pending.value.length > 0 && pending.value.every((tool) => !!decisions[tool.toolCallId]),
)

function resetDecisions(): void {
  for (const key of Object.keys(decisions)) delete decisions[key]
}

watch(
  () => props.interaction.interruptId,
  () => resetDecisions(),
)

function decide(toolCallId: string, decision: 'approve' | 'reject'): void {
  if (locked.value || !props.interaction.interruptId) return
  decisions[toolCallId] = decision
  // A single pending call needs no confirmation step.
  if (pending.value.length === 1) sendDecisions()
}

function sendDecisions(): void {
  if (!allDecided.value || locked.value) return
  emit(
    'approve',
    pending.value.map((tool) => ({
      tool_call_id: tool.toolCallId,
      decision: decisions[tool.toolCallId],
    })),
  )
}

/* ---------------------------------------------------------------- human input */

interface FieldState {
  text: string
  selected: string[]
  bool: boolean | null
  customActive: boolean
}

const states = reactive<Record<string, FieldState>>({})
const attemptSend = reactive({ value: false })

const fields = computed<InputField[]>(() => {
  const declared = props.interaction.humanInput?.fields ?? []
  if (declared.length) return declared
  return [
    { id: 'text', type: 'textarea', label: '回复', required: true, placeholder: '回复 Agent', options: [] },
  ]
})

function signature(): string {
  return JSON.stringify([props.interaction.interruptId, fields.value.map((field) => [field.id, field.type])])
}

function resetFields(): void {
  for (const key of Object.keys(states)) delete states[key]
  for (const field of fields.value) {
    states[field.id] = { text: '', selected: [], bool: null, customActive: false }
  }
  attemptSend.value = false
}

watch(signature, resetFields, { immediate: true })

function normalOptions(field: InputField) {
  return (field.options ?? []).filter((option) => !option.allowsCustomText)
}

function customOption(field: InputField) {
  return (field.options ?? []).find((option) => option.allowsCustomText) ?? null
}

function stateOf(field: InputField): FieldState {
  if (!states[field.id]) states[field.id] = { text: '', selected: [], bool: null, customActive: false }
  return states[field.id]
}

function toggleSelect(field: InputField, value: string): void {
  const state = stateOf(field)
  if (field.type === 'single_select') state.customActive = false
  if (field.type === 'single_select') {
    state.selected = state.selected[0] === value ? [] : [value]
    return
  }
  const index = state.selected.indexOf(value)
  if (index >= 0) state.selected.splice(index, 1)
  else state.selected.push(value)
}

function toggleCustom(field: InputField): void {
  const state = stateOf(field)
  state.customActive = !state.customActive
  if (state.customActive && field.type === 'single_select') state.selected = []
}

function valueOf(field: InputField): unknown {
  const state = stateOf(field)
  switch (field.type) {
    case 'text':
    case 'textarea':
      return state.text.trim()
    case 'single_select':
      return state.customActive ? state.text.trim() : (state.selected[0] ?? '')
    case 'multi_select': {
      const values = [...state.selected]
      const custom = state.customActive ? state.text.trim() : ''
      if (custom) values.push(custom)
      return values
    }
    case 'boolean':
      return state.bool
    default:
      return state.text.trim()
  }
}

function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true
  if (Array.isArray(value)) return value.length === 0
  return typeof value === 'string' ? !value.trim() : false
}

const missing = computed(() =>
  fields.value.filter((field) => field.required && isEmpty(valueOf(field))).map((field) => field.label || field.id),
)

const canSendHuman = computed(
  () => !locked.value && missing.value.length === 0 && fields.value.length > 0,
)

function submitHuman(): void {
  attemptSend.value = true
  if (!canSendHuman.value) return
  const values: Record<string, unknown> = {}
  for (const field of fields.value) {
    const value = valueOf(field)
    if (!field.required && isEmpty(value)) continue
    values[field.id] = value
  }
  emit('human', values)
}

function applyRecommendation(): void {
  const recommendation = props.interaction.humanInput?.recommendation
  if (!recommendation) return
  const raw = String(recommendation.value ?? '')
  const target =
    fields.value.find((field) => (field.options ?? []).some((option) => option.value === raw)) ??
    fields.value.find((field) => field.type === 'text' || field.type === 'textarea') ??
    fields.value[0]
  if (!target) return
  const state = stateOf(target)
  if (target.type === 'boolean') {
    state.bool = raw === 'true' || raw === 'yes' || raw === '是'
    return
  }
  const option = (target.options ?? []).find((entry) => entry.value === raw && !entry.allowsCustomText)
  if (option && (target.type === 'single_select' || target.type === 'multi_select')) {
    state.selected = [option.value]
    state.customActive = false
    return
  }
  state.text = raw
  if (target.type === 'single_select' || target.type === 'multi_select') state.customActive = true
}
</script>

<template>
  <!-- tool approval: one flat row per pending call -->
  <div v-if="interaction.kind === 'approvals'" class="log" role="region" aria-label="工具审批">
    <div class="log-row">
      <span class="verb">需要批准</span>
      <span class="detail">批准后将运行 {{ pending.length }} 个工具调用</span>
    </div>
    <div v-for="tool in pending" :key="tool.toolCallId" class="log-row">
      <span class="verb">{{ summarizeCall(tool.name, tool.args).verb }}</span>
      <span class="detail" :title="summarizeCall(tool.name, tool.args).detail">
        {{ summarizeCall(tool.name, tool.args).detail || tool.name }}
      </span>
      <span class="inline-act">
        <button
          class="act-btn"
          :class="{ 'is-on': decisions[tool.toolCallId] === 'approve' }"
          type="button"
          :disabled="locked"
          :aria-label="`批准 ${tool.name}`"
          @click="decide(tool.toolCallId, 'approve')"
        >
          批准
        </button>
        <button
          class="act-btn"
          :class="{ 'is-on': decisions[tool.toolCallId] === 'reject' }"
          type="button"
          :disabled="locked"
          :aria-label="`拒绝 ${tool.name}`"
          @click="decide(tool.toolCallId, 'reject')"
        >
          拒绝
        </button>
      </span>
    </div>
    <div v-if="pending.length > 1" class="log-row">
      <span class="detail">提交前需要为每个待批调用做出决定。</span>
      <span class="inline-act">
        <button class="act-btn is-primary" type="button" :disabled="!allDecided || locked" @click="sendDecisions">
          提交决定
        </button>
      </span>
    </div>
    <p v-if="submitted" class="log-notice">决定已提交——等待运行继续…</p>
    <p v-if="rejected" class="log-err">{{ rejected }}</p>
  </div>

  <!-- human input: question + compact inline fields -->
  <div v-else-if="interaction.kind === 'human'" class="log" role="form" aria-label="补充回答">
    <div class="log-row">
      <span class="verb">{{ interaction.humanInput?.title || 'Agent 需要你的输入' }}</span>
      <span class="detail">
        {{ interaction.humanInput?.question || interaction.humanInput?.reason || '' }}
      </span>
      <span v-if="interaction.humanInput?.recommendation" class="inline-act">
        <button class="act-btn" type="button" :disabled="locked" @click="applyRecommendation">
          采用建议
        </button>
      </span>
    </div>
    <div v-for="(item, index) in interaction.humanInput?.impact ?? []" :key="index" class="log-row">
      <span class="detail">{{ item }}</span>
    </div>

    <div v-for="field in fields" :key="field.id" class="log-field">
      <label class="field-label" :for="`input-${interaction.runId}-${field.id}`">{{ field.label || field.id }}{{ field.required ? ' *' : '' }}</label>

      <template v-if="field.type === 'text' || field.type === 'textarea'">
        <component
          :is="field.type === 'textarea' ? 'textarea' : 'input'"
          :id="`input-${interaction.runId}-${field.id}`"
          class="log-input"
          :type="field.type === 'text' ? 'text' : undefined"
          :placeholder="field.placeholder"
          :value="stateOf(field).text"
          :disabled="locked"
          @input="stateOf(field).text = ($event.target as HTMLInputElement).value"
        />
      </template>

      <template v-else-if="field.type === 'boolean'">
        <span :id="`input-${interaction.runId}-${field.id}`" class="log-chips" role="group" :aria-label="field.label">
          <button
            class="chip-toggle"
            :class="{ 'is-on': stateOf(field).bool === true }"
            type="button"
            :disabled="locked"
            @click="stateOf(field).bool = true"
          >
            是
          </button>
          <button
            class="chip-toggle"
            :class="{ 'is-on': stateOf(field).bool === false }"
            type="button"
            :disabled="locked"
            @click="stateOf(field).bool = false"
          >
            否
          </button>
        </span>
      </template>

      <template v-else>
        <span :id="`input-${interaction.runId}-${field.id}`" class="log-chips" role="group" :aria-label="field.label">
          <button
            v-for="option in normalOptions(field)"
            :key="option.value"
            class="chip-toggle"
            :class="{ 'is-on': (field.type === 'multi_select' || !stateOf(field).customActive) && stateOf(field).selected.includes(option.value) }"
            type="button"
            :title="option.description"
            :disabled="locked"
            @click="toggleSelect(field, option.value)"
          >
            {{ option.label }}
          </button>
          <button
            v-if="customOption(field)"
            class="chip-toggle"
            :class="{ 'is-on': stateOf(field).customActive }"
            type="button"
            :disabled="locked"
            @click="toggleCustom(field)"
          >
            {{ customOption(field)?.label || '其他：' }}
          </button>
        </span>
        <input
          v-if="customOption(field) && stateOf(field).customActive"
          class="log-input"
          style="max-width: 180px"
          type="text"
          :aria-label="`${field.label} 自定义答案`"
          placeholder="自定义答案"
          :value="stateOf(field).text"
          :disabled="locked"
          @input="stateOf(field).text = ($event.target as HTMLInputElement).value"
        />
      </template>
    </div>

    <div class="log-row">
      <span v-if="attemptSend.value && missing.length" class="log-err">
        必填：{{ missing.join('、') }}
      </span>
      <span v-else-if="submitted" class="log-notice">回答已提交——等待运行继续…</span>
      <span v-else-if="rejected" class="log-err">{{ rejected }}</span>
      <span class="inline-act">
        <button class="act-btn is-primary" type="button" :disabled="!canSendHuman" @click="submitHuman">
          <Icon name="arrowUp" :size="11" />
          发送
        </button>
      </span>
    </div>
  </div>

  <!-- paused / config boundary: the service waits for an explicit continue -->
  <div v-else-if="interaction.kind === 'continue'" class="log">
    <div class="log-row">
      <span class="verb">已暂停</span>
      <span class="detail">
        {{ interaction.status === 'paused' ? '已到达检查点' : '运行时配置边界' }}
      </span>
      <span class="inline-act">
        <button class="act-btn is-primary" type="button" :disabled="locked" @click="emit('continue')">
          继续
        </button>
      </span>
    </div>
  </div>
</template>

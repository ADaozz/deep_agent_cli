<script setup lang="ts">
/**
 * One run in the thread, in the shape of the real agent page:
 * user prompt card → "Environment ready" + "Worked for …" with a flat work log
 * (inline approvals / questions) → plain markdown answer → copy / fork footer.
 */
import { computed, ref, watch } from 'vue'
import Icon from './Icon.vue'
import InlineInteraction from './InlineInteraction.vue'
import MarkdownView from './MarkdownView.vue'
import type { ApprovalDecision } from '../api/types'
import type { InteractionModel, RunRecord } from '../lib/runView'
import { buildTimeline, longAge } from '../lib/timeline'
import { continueRun, submitApprovals, submitHumanInput } from '../store/app'

const props = defineProps<{
  record: RunRecord
  isLast: boolean
  interaction: InteractionModel | null
}>()

const view = computed(() => buildTimeline(props.record))
const terminal = computed(() =>
  ['completed', 'failed', 'cancelled', 'interrupted'].includes(props.record.run.status),
)

const open = ref(!terminal.value)
const promptOpen = ref(false)
const expanded = ref<Record<string, boolean>>({})
const copied = ref(false)
const interactionBusy = ref(false)

watch(
  () => props.record.run.status,
  (status) => {
    const done = ['completed', 'failed', 'cancelled', 'interrupted'].includes(status)
    open.value = !done
  },
)

const longPrompt = computed(
  () => view.value.prompt.length > 220 || view.value.prompt.split('\n').length > 3,
)

function toggleRow(key: string): void {
  expanded.value = { ...expanded.value, [key]: !expanded.value[key] }
}

async function copyAnswer(): Promise<void> {
  const text = view.value.answer
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 1400)
  } catch {
    copied.value = false
  }
}

const submitted = computed(
  () =>
    !!props.interaction?.interruptId &&
    props.record.view.submittedInterruptId === props.interaction.interruptId,
)

async function onApprove(decisions: ApprovalDecision[]): Promise<void> {
  const interaction = props.interaction
  if (!interaction?.interruptId) return
  if (interactionBusy.value) return
  interactionBusy.value = true
  try { await submitApprovals(interaction.runId, interaction.interruptId, decisions) } finally { interactionBusy.value = false }
}

async function onHuman(values: Record<string, unknown>): Promise<void> {
  const interaction = props.interaction
  if (!interaction?.interruptId) return
  if (interactionBusy.value) return
  interactionBusy.value = true
  try { await submitHumanInput(interaction.runId, interaction.interruptId, values) } finally { interactionBusy.value = false }
}

async function onContinue(): Promise<void> {
  const interaction = props.interaction
  if (!interaction?.interruptId) return
  if (interactionBusy.value) return
  interactionBusy.value = true
  try { await continueRun(interaction.runId, interaction.interruptId) } finally { interactionBusy.value = false }
}
</script>

<template>
  <div class="run-block" :data-run-id="record.id">
    <template v-if="view.prompt">
      <div class="user-card" :style="longPrompt && !promptOpen ? { maxHeight: '132px', overflow: 'hidden' } : undefined">
        {{ view.prompt }}
      </div>
      <button v-if="longPrompt" class="note-row is-click" type="button" style="margin: -8px 0 0 9px" @click="promptOpen = !promptOpen">
        {{ promptOpen ? '收起' : '展开' }}
        <Icon :name="promptOpen ? 'chevronUp' : 'chevronDown'" :size="11" />
      </button>
    </template>

    <div class="run-meta">
      <span class="note-row">环境就绪</span>
      <button class="note-row is-click" type="button" @click="open = !open">
        {{ view.worked }}
        <Icon class="chev" :name="open ? 'chevronDown' : 'chevronRight'" :size="11" />
      </button>
    </div>

    <div v-if="open" class="log">
      <template v-for="row in view.rows" :key="row.key">
        <div v-if="row.kind === 'notice'" class="log-notice">{{ row.text }}</div>

        <div v-else-if="row.kind === 'prose'" class="log-prose">
          <MarkdownView :text="row.text ?? ''" />
        </div>

        <template v-else>
          <button
            class="log-row is-click"
            :class="{ 'is-error': row.isError }"
            type="button"
            :title="row.kind === 'tool' ? row.detail : '显示思考过程'"
            @click="toggleRow(row.key)"
          >
            <span class="verb">{{ row.verb }}</span>
            <span v-if="row.detail" class="detail">{{ row.detail }}</span>
            <span v-if="row.tail" class="tail">{{ expanded[row.key] ? '⌃' : row.tail }}</span>
          </button>
          <template v-if="expanded[row.key]">
            <pre v-if="row.kind === 'tool'" class="log-out">{{ row.tool?.output || row.tool?.content || '无输出' }}</pre>
            <div v-else class="log-row">
              <span class="detail wrap">{{ row.text }}</span>
            </div>
          </template>
        </template>
      </template>
    </div>

    <InlineInteraction
      v-if="isLast && interaction"
      :interaction="interaction"
      :submitted="submitted"
      :rejected="record.view.rejected"
      :busy="interactionBusy"
      @approve="onApprove"
      @human="onHuman"
      @continue="onContinue"
    />

    <p v-if="record.run.result?.error" class="log-err" style="margin: 8px 0 0 9px">
      {{ record.run.result.error }}
    </p>

    <div v-if="view.answer" class="answer">
      <MarkdownView :text="view.answer" />
      <div class="msg-foot">
        <button class="ico" type="button" :title="copied ? '已复制' : '复制回答'" @click="copyAnswer">
          <Icon :name="copied ? 'check' : 'copy'" :size="13" />
        </button>
        <span class="ago">{{ longAge(record.run.created_at) }}</span>
      </div>
    </div>
    <div v-else-if="view.running && !interaction" class="log-notice" style="margin: 8px 0 0 9px">
      运行中…
    </div>
  </div>
</template>

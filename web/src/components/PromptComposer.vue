<script setup lang="ts">
/**
 * The prompt input in both shapes the real page uses:
 *   card (home)  → rounded 12 card, 640×120, textarea above the control bar
 *   pill (thread) → 44px pill with the textarea inline between + and the buttons
 *
 * Left: circular "+" (Files / Skills / MCP) and the model label. Right: mic and
 * the round black send / stop button. Model selection uses the service catalog
 * and session settings; attachments are configured on the service.
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import Popover from './Popover.vue'
import { models } from '../store/models'
import ModelPicker from './ModelPicker.vue'
import { app, currentRecord } from '../store/app'

const props = defineProps<{
  modelValue: string
  placeholder: string
  /** A run is active: the round button becomes Stop. */
  running: boolean
  /** A request is in flight (send/stop/approval). */
  busy: boolean
  /** The prompt can be typed but not sent yet (kept for the tooltip only). */
  disabled: boolean
  variant?: 'card' | 'pill'
}>()

const emit = defineEmits<{
  'update:modelValue': [string]
  submit: [string]
  stop: []
}>()

const area = ref<HTMLTextAreaElement | null>(null)
const plusOpen = ref(false)
const modelProject = computed(() => app.view === 'thread' ? currentRecord.value?.session.project_id || '' : app.draftProjectId || app.projects[0]?.id || '')
const modelSession = computed(() => app.view === 'thread' ? app.sessionId : '')

const canSend = computed(() => !props.disabled && !props.running && !props.busy && !models.changing[modelSession.value] && props.modelValue.trim().length > 0)
const pill = computed(() => props.variant === 'pill')

function resize(): void {
  const el = area.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, pill.value ? 160 : 220)}px`
}

watch(() => props.modelValue, () => nextTick(resize))
onMounted(resize)

function onInput(event: Event): void {
  const target = event.target as HTMLTextAreaElement
  emit('update:modelValue', target.value)
  nextTick(resize)
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  event.preventDefault()
  if (props.running) return
  if (!canSend.value) return
  emit('submit', props.modelValue)
}

function onRound(): void {
  if (props.running) {
    emit('stop')
    return
  }
  if (!canSend.value) return
  emit('submit', props.modelValue)
}

</script>

<template>
  <div class="composer" :class="{ 'is-pill': pill }">
    <template v-if="!pill">
      <textarea
        ref="area"
        aria-label="向 Agent 输入指令"
        rows="1"
        :value="modelValue"
        :placeholder="placeholder"
        spellcheck="false"
        @input="onInput"
        @keydown="onKeydown"
      />

      <div class="composer-bar">
        <span class="anchor">
          <button class="round round-fill" type="button" title="添加上下文" @click="plusOpen = !plusOpen">
            <Icon name="plus" :size="12" />
          </button>
          <Popover v-if="plusOpen" align="up" @close="plusOpen = false">
            <button class="pop-item" type="button" @click="plusOpen = false">
              <Icon name="file" :size="13" />
              <span class="grow">文件</span>
            </button>
            <button class="pop-item" type="button" @click="plusOpen = false">
              <Icon name="skill" :size="13" />
              <span class="grow">技能</span>
            </button>
            <button class="pop-item" type="button" @click="plusOpen = false">
              <Icon name="mcp" :size="13" />
              <span class="grow">MCP 服务器</span>
            </button>
            <div class="pop-sep" />
            <p class="pop-note">
              附件、技能与 MCP 服务器在服务端配置；HTTP API 只接受文本。
            </p>
          </Popover>
        </span>

        <ModelPicker :project-id="modelProject" :session-id="modelSession" :disabled="disabled || running || busy" :end="pill" />

        <span class="spacer" />

        <button class="round" type="button" title="本控制台不支持语音输入">
          <Icon name="mic" :size="15" />
        </button>
        <button
          class="round round-send"
          type="button"
          :disabled="busy || (!running && !canSend)"
          :title="running ? '停止运行' : disabled ? '请先连接服务' : '发送'"
          @click="onRound"
        >
          <Icon v-if="running" name="stop" :size="11" />
          <Icon v-else name="waveform" :size="12" :stroke-width="1.2" />
        </button>
      </div>
    </template>

    <div v-else class="composer-row">
      <span class="anchor">
        <button class="round round-fill" type="button" title="添加上下文" @click="plusOpen = !plusOpen">
          <Icon name="plus" :size="12" />
        </button>
        <Popover v-if="plusOpen" align="up" @close="plusOpen = false">
          <button class="pop-item" type="button" @click="plusOpen = false">
            <Icon name="file" :size="13" />
            <span class="grow">文件</span>
          </button>
          <button class="pop-item" type="button" @click="plusOpen = false">
            <Icon name="skill" :size="13" />
            <span class="grow">技能</span>
          </button>
          <button class="pop-item" type="button" @click="plusOpen = false">
            <Icon name="mcp" :size="13" />
            <span class="grow">MCP 服务器</span>
          </button>
          <div class="pop-sep" />
          <p class="pop-note">
            附件、技能与 MCP 服务器在服务端配置；HTTP API 只接受文本。
          </p>
        </Popover>
      </span>

      <textarea
        ref="area"
        aria-label="向 Agent 输入指令"
        rows="1"
        :value="modelValue"
        :placeholder="placeholder"
        spellcheck="false"
        @input="onInput"
        @keydown="onKeydown"
      />

      <div class="right">
        <ModelPicker :project-id="modelProject" :session-id="modelSession" :disabled="disabled || running || busy" :end="pill" />
        <button class="round" type="button" title="本控制台不支持语音输入">
          <Icon name="mic" :size="15" />
        </button>
        <button
          class="round round-send"
          type="button"
          :disabled="busy || (!running && !canSend)"
          :title="running ? '停止运行' : disabled ? '请先连接服务' : '发送'"
          @click="onRound"
        >
          <Icon v-if="running" name="stop" :size="11" />
          <Icon v-else name="waveform" :size="12" :stroke-width="1.2" />
        </button>
      </div>
    </div>
  </div>
</template>

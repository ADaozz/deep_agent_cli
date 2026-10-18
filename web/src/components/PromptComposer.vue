<script setup lang="ts">
/**
 * The prompt input in both shapes the real page uses:
 *   card (home)  → rounded 12 card, 640×120, textarea above the control bar
 *   pill (thread) → 44px pill with the textarea inline between + and the buttons
 *
 * Left: circular "+" (files / images / Skills), permission mode and model label. Right: the round black send / stop button. Model selection uses the service catalog
 * and session settings; attachments are plain path references.
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import AttachmentPathDialog from './AttachmentPathDialog.vue'
import type { PathReference } from '../store/drafts'
import Popover from './Popover.vue'
import { models } from '../store/models'
import PermissionPicker from './PermissionPicker.vue'
import SkillsMenu from './SkillsMenu.vue'
import type { SkillReference } from '../api/types'
import ModelPicker from './ModelPicker.vue'
import { app, currentRecord } from '../store/app'

const props = defineProps<{
  modelValue: string
  paths: PathReference[]
  skills: SkillReference[]
  draftKey: string
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
  'update:paths': [PathReference[]]
  'update:skills': [SkillReference[]]
  submit: [string]
  stop: []
}>()

const area = ref<HTMLTextAreaElement | null>(null)
const plusOpen = ref(false)
const skillsOpen = ref(false)
watch(plusOpen, () => { skillsOpen.value = false })
function selectSkill(skill: SkillReference): void {
  if (!props.skills.some(item => item.path === skill.path)) emit('update:skills', [...props.skills, skill])
  plusOpen.value = false
}
const pathKind = ref<'file' | 'image' | null>(null)
watch(() => props.draftKey, () => { pathKind.value = null; plusOpen.value = false })
function pickPath(kind: 'file' | 'image'): void { plusOpen.value = false; pathKind.value = kind }
function addPaths(paths: PathReference[]): void {
  emit('update:paths', [...props.paths, ...paths.filter(p => !props.paths.some(existing => existing.path === p.path))])
  pathKind.value = null
}
const modelProject = computed(() => app.view === 'thread' ? currentRecord.value?.session.project_id || '' : app.draftProjectId || app.projects[0]?.id || '')
const modelSession = computed(() => app.view === 'thread' ? app.sessionId : '')

const canSend = computed(() => !props.disabled && !props.running && !props.busy && !models.changing[modelSession.value] && (props.modelValue.trim().length > 0 || props.paths.length > 0 || props.skills.length > 0))
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
  <div class="composer" :class="{ 'is-pill': pill, 'has-paths': paths.length || skills.length }">
    <AttachmentPathDialog v-if="pathKind" :kind="pathKind" @close="pathKind = null" @selected="addPaths" />
    <div v-if="paths.length || skills.length" class="attachment-blocks" aria-label="附件路径">
      <div v-for="skill in skills" :key="skill.path" class="attachment-block" :title="skill.description">
        <span>Skill: {{ skill.name }}</span><button class="ico" type="button" :aria-label="'移除 Skill ' + skill.name" @click="emit('update:skills', skills.filter(s => s.path !== skill.path))"><Icon name="close" :size="12" /></button>
      </div>
      <div v-for="item in paths" :key="item.path" class="attachment-block" :title="item.path">
        <Icon :name="item.kind === 'image' ? 'image' : 'file'" :size="16" />
        <span>{{ item.path }}</span>
        <button class="ico" type="button" :aria-label="'移除 ' + item.path" @click="emit('update:paths', paths.filter(p => p.path !== item.path))"><Icon name="close" :size="12" /></button>
      </div>
    </div>
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
            <SkillsMenu v-if="skillsOpen" :project-id="modelProject" :disabled="disabled || running || busy" @back="skillsOpen = false" @selected="selectSkill" />
            <template v-else>
              <button class="pop-item" type="button" @click="pickPath('file')">
                <Icon name="file" :size="13" />
                <span class="grow">上传文件</span>
              </button>
              <button class="pop-item" type="button" @click="pickPath('image')">
                <Icon name="image" :size="13" />
                <span class="grow">上传图片</span>
              </button>
              <button class="pop-item" type="button" :disabled="disabled || running || busy" @click="skillsOpen = true"><span class="grow">Skills</span><Icon name="chevronRight" :size="11" /></button>
            </template>
          </Popover>
        </span>

        <PermissionPicker :project-id="modelProject" :session-id="modelSession" :disabled="disabled || running || busy" />
        <ModelPicker :project-id="modelProject" :session-id="modelSession" :disabled="disabled || running || busy" :end="pill" />

        <span class="spacer" />
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
          <SkillsMenu v-if="skillsOpen" :project-id="modelProject" :disabled="disabled || running || busy" @back="skillsOpen = false" @selected="selectSkill" />
          <template v-else>
            <button class="pop-item" type="button" @click="pickPath('file')">
              <Icon name="file" :size="13" />
              <span class="grow">上传文件</span>
            </button>
            <button class="pop-item" type="button" @click="pickPath('image')">
              <Icon name="image" :size="13" />
              <span class="grow">上传图片</span>
            </button>
            <button class="pop-item" type="button" :disabled="disabled || running || busy" @click="skillsOpen = true"><span class="grow">Skills</span><Icon name="chevronRight" :size="11" /></button>
          </template>
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
        <PermissionPicker :project-id="modelProject" :session-id="modelSession" :disabled="disabled || running || busy" />
        <ModelPicker :project-id="modelProject" :session-id="modelSession" :disabled="disabled || running || busy" :end="pill" />
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

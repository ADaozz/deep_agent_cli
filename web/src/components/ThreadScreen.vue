<script setup lang="ts">
/**
 * Screen (B): a session thread. Borderless header, a centered ~720px column of
 * runs, a status chip and the sticky composer at the bottom, plus the optional
 * right-side Changes panel.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import Popover from './Popover.vue'
import PromptComposer from './PromptComposer.vue'
import { useDraft, readDraft, draftPrompt, clearSentDraft } from '../store/drafts'
import SidePanel from './SidePanel.vue'
import WorkLog from './WorkLog.vue'
import {
  app,
  cancelRun,
  currentInteraction,
  currentRecord,
  currentRow,
  currentRunActive,
  currentRuns,
  latestRecord,
  loadSessionHistory,
  refreshCurrent,
  resumeSession,
  runBannerAction,
  submitText,
} from '../store/app'
import { refreshGit, workspaceGit } from '../store/git'
import { projectLabel } from '../store/settings'
import { STATUS_UI } from '../lib/timeline'
import type { RunStatus } from '../api/types'

defineProps<{ panelOpen: boolean }>()
const emit = defineEmits<{ 'toggle-panel': [] }>()

const draftKey = computed(() => 'thread:' + app.sessionId)
const { text, paths, skills } = useDraft(draftKey)
const menu = ref(false)
const body = ref<HTMLElement | null>(null)
const copied = ref(false)

const sessionId = computed(() => app.sessionId)
const runs = computed(() => currentRuns.value)
const lastIndex = computed(() => runs.value.length - 1)

const title = computed(() => currentRow.value?.title || '会话')
const repo = computed(() =>
  projectLabel(currentRecord.value?.session.project_id ?? app.draftProjectId),
)

const status = computed<RunStatus>(() => latestRecord.value?.run.status ?? 'completed')
const active = computed(() => currentRunActive.value)
const chip = computed(() => (active.value ? STATUS_UI[status.value] : null))
const interrupted = computed(() => status.value === 'interrupted')

const bannerKind = computed(() => {
  if (!app.banner) return 'is-bad'
  return app.banner.kind === 'error' ? 'is-bad' : app.banner.kind === 'warn' ? 'is-warn' : 'is-idle'
})

/* elapsed label for the running chip */
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

const elapsed = computed(() => {
  const last = latestRecord.value
  // Only a run that is actually working gets a ticking stopwatch; waiting for the
  // user or a checkpoint is not elapsed work.
  if (!active.value || !last || status.value !== 'running') return ''
  const start = Date.parse(last.run.created_at)
  if (Number.isNaN(start)) return ''
  const seconds = Math.max(0, Math.floor((now.value - start) / 1000))
  const minutes = Math.floor(seconds / 60)
  return minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`
})

onMounted(() => {
  timer = setInterval(() => {
    now.value = Date.now()
    if (Math.floor(now.value / 1000) % 5 === 0) void refreshGit(currentRecord.value?.session.project_id || '')
  }, 1000)
  void refreshGit(currentRecord.value?.session.project_id || '')
  scrollToBottom(false)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

function scrollToBottom(smooth = true): void {
  const el = body.value
  if (!el) return
  el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
}

watch(
  () => [
    sessionId.value,
    runs.value.length,
    latestRecord.value?.view.items.length ?? 0,
    latestRecord.value?.view.blocks.length ?? 0,
    latestRecord.value?.view.finalized ?? false,
  ],
  () => { void refreshGit(currentRecord.value?.session.project_id || ''); scrollToBottom() },
)

async function send(): Promise<void> {
  const key = draftKey.value
  const snapshot = JSON.stringify(readDraft(key))
  const ok = await submitText(draftPrompt(readDraft(key), false), readDraft(key).paths.map(item => item.path))
  if (ok) {
    clearSentDraft(key, snapshot)
    scrollToBottom()
  }
}

function stop(): void {
  const last = latestRecord.value
  if (!last) return
  void cancelRun(last.run.id)
}

async function resume(): Promise<void> {
  await resumeSession()
  // The new run block is mounted after the snapshot lands; wait a frame so the
  // scroll height includes the fresh prompt card.
  window.setTimeout(() => scrollToBottom(), 60)
}

async function copySession(): Promise<void> {
  if (!sessionId.value) return
  try {
    await navigator.clipboard.writeText(sessionId.value)
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 1400)
  } catch {
    copied.value = false
  }
}
</script>

<template>
  <section class="thread-screen">
    <div class="thread-main">
      <header class="thread-head">
        <div class="crumbs">
          <span class="title" :title="sessionId ?? undefined">{{ title }}</span>
          <button class="repo-chip" type="button" :title="repo" @click="app.settingsOpen = true">
            <Icon name="cloud" :size="13" />
            <span class="chip-text">{{ repo }}</span>
          </button>
        </div>

        <div class="head-actions">
          <span class="anchor">
            <button class="ico" type="button" title="更多" @click="menu = !menu">
              <Icon name="dots" :size="15" :stroke-width="2" />
            </button>
            <Popover v-if="menu" align="down" end @close="menu = false">
              <button class="pop-item" type="button" @click="refreshCurrent(); menu = false">
                <Icon name="refresh" :size="13" />
                <span class="grow">重新加载此会话</span>
              </button>
              <button class="pop-item" type="button" @click="copySession(); menu = false">
                <Icon name="copy" :size="13" />
                <span class="grow">{{ copied ? '已复制会话 ID' : '复制会话 ID' }}</span>
              </button>
              <button class="pop-item" type="button" @click="app.settingsOpen = true; menu = false">
                <Icon name="gear" :size="13" />
                <span class="grow">连接设置…</span>
              </button>
              <div class="pop-sep" />
              <p class="pop-note">
                重命名、分享与删除会话不在 deep-agent API 范围内。
              </p>
            </Popover>
          </span>
          <button
            class="ico"
            :class="{ 'is-on': panelOpen }"
            type="button"
            :title="panelOpen ? '隐藏面板' : '显示变更面板'"
            @click="emit('toggle-panel')"
          >
            <Icon name="panelRight" :size="15" />
          </button>
        </div>
      </header>

      <div ref="body" class="thread-body">
        <div class="thread-col">
          <p v-if="!runs.length" class="log-notice">
            此会话还没有运行——发送一条跟进即可开始。
          </p>
          <button v-if="app.history[sessionId]?.cursor" class="btn" :disabled="app.history[sessionId]?.loading" @click="loadSessionHistory(sessionId, true)">加载更早运行</button>
          <p v-if="app.history[sessionId]?.loading" class="log-notice">正在加载运行历史…</p>
          <WorkLog
            v-for="(record, index) in runs"
            :key="record.run.id"
            :record="record"
            :is-last="index === lastIndex"
            :interaction="index === lastIndex ? currentInteraction : null"
          />
        </div>
      </div>

      <div class="thread-foot">
        <div class="foot-col">
          <div v-if="interrupted" class="banner">
            <Icon name="alert" :size="13" />
            <span class="grow">运行已中断。恢复会在同一会话上新建一次运行。</span>
            <button class="act-btn" type="button" :disabled="app.busy.resume" @click="resume()">
              恢复
            </button>
          </div>

          <div v-if="app.banner" class="banner" :class="bannerKind" role="alert">
            <Icon name="alert" :size="13" />
            <span class="grow">{{ app.banner.text }}<small v-if="app.banner.hint" class="banner-hint">{{ app.banner.hint }}</small></span>
            <button
              v-for="action in app.banner.actions"
              :key="action.key"
              class="act-btn"
              type="button"
              @click="runBannerAction(action.key)"
            >
              {{ action.label }}
            </button>
          </div>

          <div class="cloud-cta-row">
            <button
              class="cloud-cta"
              type="button"
              @click="app.settingsOpen = true"
            >
              <Icon name="cloud" :size="12" />
              连接设置
            </button>

            <span v-if="chip" class="status-chip" :class="`is-${chip.tone}`">
              <span class="dot" />
              {{ chip.label }}
              <span v-if="elapsed" class="soft">{{ elapsed }}</span>
            </span>
          </div>

          <PromptComposer
            v-model="text"
            v-model:paths="paths"
            v-model:skills="skills"
            :draft-key="draftKey"
            variant="pill"
            placeholder="追加跟进"
            :running="active"
            :busy="app.busy.submit || app.busy.cancel || status === 'cancelling'"
            :disabled="!app.conn.authorized || interrupted"
            @submit="send"
            @stop="stop"
          />
        </div>
      </div>
    </div>

    <SidePanel
      v-if="panelOpen"
      :git="workspaceGit[currentRecord?.session.project_id || '']"
      :project-id="currentRecord?.session.project_id || ''"
      :title="repo"
      :subtitle="sessionId ? sessionId.slice(0, 8) : ''"
      @close="emit('toggle-panel')"
    />
  </section>
</template>

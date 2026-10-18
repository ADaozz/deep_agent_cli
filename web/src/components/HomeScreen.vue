<script setup lang="ts">
/**
 * Home / New Chat: repository picker, the 640px prompt card, the two pills and
 * the "Recent agents" list. Everything is derived from `/v1/projects`,
 * `/v1/sessions` and the locally recorded runs.
 */
import { computed, ref } from 'vue'
import Icon from './Icon.vue'
import Popover from './Popover.vue'
import PromptComposer from './PromptComposer.vue'
import { app, openSession, sendFromHome, sessionRows, runBannerAction } from '../store/app'
import { collectChanges, longAge } from '../lib/timeline'
import { models } from '../store/models'
import { projectLabel, settings } from '../store/settings'

const text = ref('')
const draftOpen = ref(false)
const cloudOpen = ref(false)
const flashText = ref('')

const recent = computed(() => sessionRows.value.slice(0, 4))

const draftLabel = computed(() =>
  app.draftProjectId ? projectLabel(app.draftProjectId) : '从零开始',
)

function flash(message: string): void {
  flashText.value = message
  window.setTimeout(() => {
    if (flashText.value === message) flashText.value = ''
  }, 4000)
}

function pickProject(projectId: string): void {
  app.draftProjectId = projectId
  draftOpen.value = false
}

async function submit(value: string): Promise<void> {
  const ok = await sendFromHome(value)
  if (ok) text.value = ''
}

function thumbLabel(tone: string): string {
  if (tone === 'ok') return '已完成'
  if (tone === 'run') return '运行中'
  if (tone === 'wait') return '等待中'
  if (tone === 'bad') return '失败'
  return '空闲'
}

function thumbClass(tone: string): string {
  if (tone === 'ok') return 'is-purple'
  if (tone === 'run') return 'is-green'
  return ''
}

function changesOf(row: (typeof recent.value)[number]) {
  return row.latest ? collectChanges([row.latest]) : []
}

function metaIcon(tone: string): string {
  if (tone === 'ok') return 'checkCircle'
  if (tone === 'wait') return 'circle'
  if (tone === 'bad') return 'xCircle'
  return 'branch'
}
</script>

<template>
  <div class="home">
    <div class="home-inner">
      <div v-if="app.banner" class="banner" role="alert">
        <span class="grow">{{ app.banner.text }}<small v-if="app.banner.hint" class="banner-hint">{{ app.banner.hint }}</small></span>
        <button v-for="action in app.banner.actions" :key="action.key" class="act-btn" @click="runBannerAction(action.key)">{{ action.label }}</button>
      </div>
      <div class="home-controls">
        <span class="anchor">
          <button class="pick" type="button" @click="draftOpen = !draftOpen">
            {{ draftLabel }}
            <Icon name="chevronDown" :size="11" />
          </button>
          <Popover v-if="draftOpen" align="down" @close="draftOpen = false">
            <div class="pop-head">运行于</div>
            <button
              class="pop-item"
              type="button"
              @click="pickProject('')"
            >
              <Icon name="file" :size="13" />
              <span class="grow">从零开始</span>
              <Icon v-if="!app.draftProjectId" name="check" :size="13" />
            </button>
            <button
              v-for="project in app.projects"
              :key="project.id"
              class="pop-item"
              type="button"
              @click="pickProject(project.id)"
            >
              <Icon name="branch" :size="13" />
              <span class="grow">{{ projectLabel(project.id) }}</span>
              <Icon v-if="app.draftProjectId === project.id" name="check" :size="13" />
            </button>
            <div class="pop-sep" />
            <p class="pop-note">
              项目来自 <code>GET /v1/projects</code>；工作副本由服务端持有。
            </p>
          </Popover>
        </span>

        <span class="anchor">
          <button class="pick" type="button" title="沙箱" @click="cloudOpen = !cloudOpen">
            <Icon name="cloud" :size="14" />
            <Icon name="chevronDown" :size="10" />
          </button>
          <Popover v-if="cloudOpen" align="down" @close="cloudOpen = false">
            <div class="pop-head">{{ settings.baseUrl }}</div>
            <button class="pop-item" type="button" @click="cloudOpen = false">
              <Icon name="cloud" :size="13" />
              <span class="grow">本地沙箱</span>
              <Icon name="check" :size="13" />
            </button>
            <div class="pop-sep" />
            <p class="pop-note">
              deep-agent serve 在自身沙箱中运行 Agent；没有云端/本地切换。
            </p>
          </Popover>
        </span>
      </div>

      <PromptComposer
        v-model="text"
        placeholder="让 Deep Agent 构建、修 bug、探索"
        :running="false"
        :busy="app.busy.submit"
        :disabled="!app.conn.authorized"
        @submit="submit"
      />

      <div class="home-pills">
        <button
          class="pill-btn"
          type="button"
          @click="flash('扩展市场属于 Cursor；deep-agent 没有插件注册表。')"
        >
          浏览扩展市场
          <Icon name="arrowUpRight" :size="12" />
        </button>
        <button
          class="pill-btn"
          type="button"
          @click="text = '请审查当前项目的安全风险，给出有代码位置依据的发现和修复建议。'"
        >
          运行安全审计
        </button>
      </div>

      <p v-if="flashText" class="home-flash">{{ flashText }}</p>

      <div class="recent">
        <button
          v-for="row in recent"
          :key="row.id"
          class="recent-row"
          type="button"
          @click="openSession(row.id)"
        >
          <span class="thumb">
            <span class="thumb-note">
              <span v-if="changesOf(row).length">
                {{ changesOf(row).length }} 个文件
              </span>
              <span v-else>{{ row.label }}</span>
            </span>
            <span class="mini-pill" :class="thumbClass(row.tone)">
              <Icon name="branch" :size="10" />
              {{ thumbLabel(row.tone) }}
            </span>
          </span>

          <span class="recent-main">
            <span class="recent-title">
              <span v-if="row.tone === 'run'" class="dot" />
              <span v-else-if="row.tone === 'wait'" class="dot is-wait" />
              <span v-else-if="row.tone === 'bad'" class="dot is-bad" />
              {{ row.title }}
            </span>
            <span class="recent-meta">
              <Icon
                :name="row.projectId ? 'branch' : metaIcon(row.tone)"
                :size="12"
                :style="row.projectId ? { color: 'var(--purple)' } : undefined"
              />
              <span v-if="models.sessions[row.id]">{{ models.sessions[row.id].model }}</span>
              <span>{{ row.projectId ? projectLabel(row.projectId) : '无仓库' }}</span>
              <span>{{ longAge(row.updatedAt) }}</span>
            </span>
          </span>
        </button>

        <p v-if="!recent.length" class="side-empty" style="padding-left: 6px">
          还没有记录。在上方描述任务，第一个会话就会出现在这里。
        </p>
      </div>
    </div>
  </div>
</template>

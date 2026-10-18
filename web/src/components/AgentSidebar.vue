<script setup lang="ts">
/**
 * Left rail: 280px, `#f3f3f3`, 32px rows. Collapse + search on top, New Chat /
 * Automations / Codebase, the grouped session list and the account row with the
 * connection settings entry point.
 */
import { computed, nextTick, ref } from 'vue'
import Icon from './Icon.vue'
import Popover from './Popover.vue'
import { app, goHome, openSession, resetRecords, sessionRows } from '../store/app'
import { shortAge } from '../lib/timeline'
import { projectLabel, settings } from '../store/settings'

const emit = defineEmits<{ collapse: [] }>()

const userMenu = ref(false)
const searchOpen = ref(false)
const searchInput = ref<HTMLInputElement | null>(null)
const query = ref('')
const inertNote = ref('')

function note(message: string): void {
  inertNote.value = message
  window.setTimeout(() => {
    if (inertNote.value === message) inertNote.value = ''
  }, 4000)
}

const rows = computed(() => {
  const q = query.value.trim().toLowerCase()
  const list = sessionRows.value
  if (!q) return list
  return list.filter(
    (row) =>
      row.title.toLowerCase().includes(q) ||
      projectLabel(row.projectId).toLowerCase().includes(q),
  )
})

const groups = computed(() => {
  const week = Date.now() - 7 * 24 * 3600 * 1000
  const recent = rows.value.filter((row) => new Date(row.createdAt).getTime() >= week)
  const earlier = rows.value.filter((row) => new Date(row.createdAt).getTime() < week)
  const out: { label: string; rows: typeof recent }[] = []
  if (recent.length) out.push({ label: '近 7 天', rows: recent })
  if (earlier.length) out.push({ label: '更早', rows: earlier })
  return out
})

async function toggleSearch(): Promise<void> {
  searchOpen.value = !searchOpen.value
  if (searchOpen.value) {
    await nextTick()
    searchInput.value?.focus()
  } else {
    query.value = ''
  }
}

function newChat(): void {
  goHome()
  query.value = ''
}

function clearLocal(): void {
  resetRecords()
  goHome()
  userMenu.value = false
}
</script>

<template>
  <aside class="side">
    <div class="side-head">
      <div class="side-head-row">
        <button class="ico" type="button" title="折叠侧栏" @click="emit('collapse')">
          <Icon name="panelLeft" :size="15" />
        </button>
        <button class="ico" type="button" title="搜索会话" @click="toggleSearch">
          <Icon name="search" :size="15" />
        </button>
      </div>

      <nav class="side-nav">
        <button class="nav-row" :class="{ 'is-active': app.view === 'home' }" type="button" @click="newChat">
          <Icon name="wand" :size="14" />
          <span class="chat-title">新对话</span>
        </button>
        <button
          class="nav-row"
          type="button"
          @click="note('自动化不属于 deep-agent serve API。')"
        >
          <Icon name="clock" :size="14" />
          <span class="chat-title">自动化</span>
        </button>
        <button
          class="nav-row"
          type="button"
          @click="note('代码库索引不属于 deep-agent serve API。')"
        >
          <Icon name="code" :size="14" />
          <span class="chat-title">代码库</span>
          <span class="badge">早期预览</span>
        </button>
      </nav>

      <div v-if="searchOpen" class="side-search">
        <Icon name="search" :size="13" />
        <input ref="searchInput" v-model="query" aria-label="搜索会话" type="text" placeholder="搜索会话" />
        <button class="ico" type="button" style="width: 20px; height: 20px" @click="toggleSearch">
          <Icon name="close" :size="12" />
        </button>
      </div>
    </div>

    <div class="side-scroll">
      <template v-for="group in groups" :key="group.label">
        <div class="side-group">
          <span>{{ group.label }}</span>
          <button
            class="ico"
            type="button"
            title="筛选会话"
            @click="toggleSearch"
          >
            <Icon name="filter" :size="13" />
          </button>
        </div>
        <div class="side-list">
          <button
            v-for="row in group.rows"
            :key="row.id"
            class="chat-row"
            :class="{ 'is-active': row.active && app.view === 'thread' }"
            type="button"
            :title="row.title"
            @click="openSession(row.id)"
          >
            <span class="chat-lead" :class="{ 'is-repo': !!row.projectId }">
              <Icon :name="row.projectId ? 'branch' : 'cloud'" :size="13" />
            </span>
            <span class="chat-title">{{ row.title }}</span>
            <span class="chat-age">{{ shortAge(row.updatedAt) }}</span>
          </button>
        </div>
      </template>

      <p v-if="!groups.length" class="side-empty">
        {{ query ? '没有匹配的会话。' : '还没有会话，从「新对话」开始。' }}
      </p>
      <p v-if="inertNote" class="side-empty">{{ inertNote }}</p>
    </div>

    <div class="side-foot">
      <button class="user-row" type="button" @click="app.settingsOpen = true">
        <span class="avatar">D</span>
        <span class="user-meta">
          <span class="user-name">Deep Agent</span>
          <span class="user-plan">{{ app.conn.authorized ? '已连接 · 本地工作区' : '未连接' }}</span>
        </span>
      </button>
      <span class="anchor">
        <button class="ico" type="button" title="更多" @click="userMenu = !userMenu">
          <Icon name="dots" :size="15" :stroke-width="2" />
        </button>
        <Popover v-if="userMenu" align="up" end @close="userMenu = false">
          <div class="pop-head">
            {{ app.conn.authorized ? '已连接' : '未连接' }} · {{ settings.baseUrl }}
          </div>
          <button class="pop-item" type="button" @click="app.settingsOpen = true; userMenu = false">
            <Icon name="gear" :size="13" />
            <span class="grow">连接设置…</span>
          </button>
          <div class="pop-sep" />
          <button class="pop-item" type="button" @click="clearLocal">
            <Icon name="archive" :size="13" />
            <span class="grow">清除本地记录</span>
          </button>
        </Popover>
      </span>
    </div>
  </aside>
</template>

<script setup lang="ts">
/**
 * Left rail with collapse/search, new chat, grouped sessions and connection settings.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import Popover from './Popover.vue'
import SessionHoverCard from './SessionHoverCard.vue'
import { refreshGit, workspaceGit } from '../store/git'
import { app, goHome, openSession, resetRecords, sessionRows } from '../store/app'
import { shortAge } from '../lib/timeline'
import { projectLabel, settings } from '../store/settings'
import { appearance, setAppearance } from '../store/appearance'

const emit = defineEmits<{ collapse: [] }>()

const userMenu = ref(false)
const appearanceMenu = ref(false)
const collapsedGroups = ref<Record<string, boolean>>({})
watch(userMenu, () => { appearanceMenu.value = false })
const searchOpen = ref(false)
const searchInput = ref<HTMLInputElement | null>(null)
const query = ref('')
const hovered = ref<{ id: string; projectId: string; left: number; top: number }>()
let openTimer: ReturnType<typeof setTimeout> | undefined
let closeTimer: ReturnType<typeof setTimeout> | undefined
function cancelClose(): void { clearTimeout(closeTimer) }
function hideHover(): void {
  clearTimeout(openTimer); clearTimeout(closeTimer); hovered.value = undefined
}
function showHover(row: { id: string; projectId: string }, event: MouseEvent | FocusEvent): void {
  clearTimeout(openTimer); cancelClose()
  const element = event.currentTarget as HTMLElement
  const rect = element.getBoundingClientRect()
  openTimer = setTimeout(() => {
    if (!element.isConnected) return
    hovered.value = { id: row.id, projectId: row.projectId,
      left: Math.max(8, Math.min(rect.right + 10, window.innerWidth - 228)),
      top: Math.max(8, Math.min(rect.top, window.innerHeight - 100)) }
    if (!workspaceGit[row.projectId]) void refreshGit(row.projectId)
  }, 250)
}
function scheduleHide(): void {
  clearTimeout(openTimer)
  closeTimer = setTimeout(hideHover, 120)
}
function hoverKey(event: KeyboardEvent): void { if (event.key === 'Escape') hideHover() }
onMounted(() => {
  window.addEventListener('resize', hideHover)
  document.addEventListener('scroll', hideHover, true)
  document.addEventListener('keydown', hoverKey)
})
onBeforeUnmount(() => {
  hideHover()
  window.removeEventListener('resize', hideHover)
  document.removeEventListener('scroll', hideHover, true)
  document.removeEventListener('keydown', hoverKey)
})
const rows = computed(() => {
  const q = query.value.trim().toLowerCase()
  const list = [...sessionRows.value].sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))
  if (!q) return list
  return list.filter(
    (row) =>
      row.title.toLowerCase().includes(q) ||
      projectLabel(row.projectId).toLowerCase().includes(q),
  )
})

const groups = computed(() => {
  const todayStart = new Date()
  todayStart.setHours(0, 0, 0, 0)
  const weekStart = new Date(todayStart)
  weekStart.setDate(weekStart.getDate() - 7)
  const time = (row: typeof rows.value[number]) => Date.parse(row.updatedAt || row.createdAt)
  const today = rows.value.filter(row => time(row) >= todayStart.getTime())
  const recent = rows.value.filter(row => time(row) < todayStart.getTime() && time(row) >= weekStart.getTime())
  const earlier = rows.value.filter(row => time(row) < weekStart.getTime())
  const out: { label: string; rows: typeof today }[] = []
  if (today.length) out.push({ label: '今天', rows: today })
  if (recent.length) out.push({ label: '最近七天', rows: recent })
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
        <button class="side-group" type="button" :aria-expanded="!collapsedGroups[group.label]" @click="hideHover(); collapsedGroups[group.label] = !collapsedGroups[group.label]">
          <Icon :name="collapsedGroups[group.label] ? 'chevronRight' : 'chevronDown'" :size="12" />
          <span>{{ group.label }}</span>
        </button>
        <div v-show="!collapsedGroups[group.label]" class="side-list">
          <button
            v-for="row in group.rows"
            :key="row.id"
            class="chat-row"
            :class="{ 'is-active': row.active && app.view === 'thread' }"
            type="button"
            :aria-describedby="hovered?.id === row.id ? 'session-hover-card' : undefined"
            @mouseenter="showHover(row, $event)"
            @mouseleave="scheduleHide"
            @focus="showHover(row, $event)"
            @blur="scheduleHide"
            @click="hideHover(); openSession(row.id)"
          >
            <span class="chat-lead" :class="{ 'is-repo': !!row.projectId }">
              <Icon :name="'file'" :size="13" />
            </span>
            <span class="chat-title">{{ row.title }}</span>
            <span class="chat-age">{{ shortAge(row.updatedAt) }}</span>
          </button>
        </div>
      </template>

      <p v-if="!groups.length" class="side-empty">
        {{ query ? '没有匹配的会话。' : '还没有会话，从「新对话」开始。' }}
      </p>
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
          <div class="appearance-menu">
            <button class="pop-item" type="button" aria-haspopup="menu" :aria-expanded="appearanceMenu" aria-controls="appearance-submenu" @click="appearanceMenu = !appearanceMenu" @keydown.right.prevent="appearanceMenu = true">
              <Icon name="appearance" :size="14" /><span class="grow">外观</span><span class="appearance-current">{{ appearance === 'dark' ? '深色' : '浅色' }}</span><Icon name="chevronRight" :size="12" />
            </button>
            <div v-if="appearanceMenu" id="appearance-submenu" class="popover appearance-submenu" role="menu" aria-label="外观" @keydown.left.prevent="appearanceMenu = false">
              <button v-for="mode in (['light', 'dark'] as const)" :key="mode" class="pop-item" type="button" role="menuitemradio" :aria-checked="appearance === mode" @click="setAppearance(mode); userMenu = false"><span class="grow">{{ mode === 'dark' ? '深色' : '浅色' }}</span><Icon v-if="appearance === mode" name="check" :size="12" /></button>
            </div>
          </div>
          <button class="pop-item" type="button" @click="clearLocal">
            <Icon name="archive" :size="13" />
            <span class="grow">清除本地记录</span>
          </button>
        </Popover>
      </span>
    </div>
    <Teleport to="body"><SessionHoverCard v-if="hovered" :project-id="hovered.projectId" :left="hovered.left" :top="hovered.top" @mouseenter="cancelClose" @mouseleave="scheduleHide" /></Teleport>
  </aside>
</template>

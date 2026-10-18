<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import Icon from './Icon.vue'
import Popover from './Popover.vue'
import { api, ApiError } from '../api/client'
import type { GitDiffLine, GitDiffScope, GitFileDiff, GitFileEntry, WorkspaceGit } from '../api/types'
import { shortPath } from '../lib/timeline'
import { refreshGit } from '../store/git'
import { apiConfig } from '../store/settings'

const props = defineProps<{
  git?: WorkspaceGit
  projectId?: string
  title: string
  subtitle: string
}>()

const emit = defineEmits<{ close: [] }>()

const tab = ref<'changes' | 'desktop' | 'terminal' | 'files'>('changes')
const sub = ref<'diff' | 'review' | 'commits'>('diff')
const scope = ref<GitDiffScope>('all')
const maximized = ref(false)
const menu = ref(false)
const copied = ref(false)
const refreshing = ref(false)
const open = ref<string[]>([])
const collapsed = reactive<Record<string, boolean>>({})
const loading = reactive<Record<string, boolean>>({})
const sawFull = reactive<Record<string, boolean>>({})
const diffs = reactive<Record<string, GitFileDiff>>({})
const generation = new Map<string, number>()

const TABS: { key: typeof tab.value; label: string }[] = [
  { key: 'changes', label: '变更' },
  { key: 'desktop', label: '桌面' },
  { key: 'terminal', label: '终端' },
  { key: 'files', label: '文件' },
]

const SUBS: { key: typeof sub.value; label: string }[] = [
  { key: 'diff', label: '差异' },
  { key: 'review', label: '审阅' },
  { key: 'commits', label: '提交' },
]

const SCOPES: { key: GitDiffScope; label: string }[] = [
  { key: 'all', label: '整体差异' },
  { key: 'staged', label: '已暂存' },
  { key: 'unstaged', label: '未暂存' },
]

const STATUS: Record<string, string> = {
  untracked: '未跟踪',
  deleted: '删除',
  added: '新增',
  renamed: '重命名',
  copied: '复制',
  modified: '修改',
}

const files = computed(() => (props.git?.available ? props.git.files || [] : []))

function isUntracked(file: GitFileEntry): boolean {
  return file.untracked === true || file.status === 'untracked'
}

const tracked = computed(() => files.value.filter((file) => {
  if (isUntracked(file)) return false
  if (scope.value === 'staged') return file.staged === true
  if (scope.value === 'unstaged') return file.unstaged === true
  return true
}))

const untrackedFiles = computed(() => (scope.value === 'unstaged' ? files.value.filter(isUntracked) : []))
const groups = computed(() => [
  { label: '', files: tracked.value },
  { label: '未跟踪', files: untrackedFiles.value },
].filter((group) => group.files.length))
const shown = computed(() => groups.value.flatMap((group) => group.files))

const emptyText = computed(() => {
  const labels = { desktop: '桌面', terminal: '终端', files: '文件' } as const
  if (tab.value !== 'changes') return `${labels[tab.value]}视图不由本控制台提供`
  if (!props.git?.available) return '工作区没有可用的 Git 仓库'
  if (sub.value !== 'diff') return '工作区没有 Git 变更'
  if (scope.value === 'staged') return '没有已暂存的变更'
  if (scope.value === 'unstaged') return '没有未暂存的变更'
  return '工作区没有 Git 变更'
})

function diffKey(path: string, full = false): string {
  return `${scope.value}:${full ? '1' : '0'}:${path}`
}

function currentDiff(path: string): GitFileDiff | undefined {
  return diffs[diffKey(path, true)] ?? diffs[diffKey(path, false)]
}

function isOpen(path: string): boolean {
  return open.value.includes(path)
}

function countLabel(file: GitFileEntry): string {
  if (file.binary) return '二进制'
  if (file.untracked || file.added == null || file.removed == null) return '暂无行数统计'
  if (scope.value !== 'all') return ''
  return `+${file.added} / -${file.removed}`
}

function lineNo(line: GitDiffLine): string {
  if (line.kind === 'meta') return ''
  const value = line.kind === 'delete' ? line.old_no : line.new_no
  return value == null ? '' : String(value)
}

function mark(kind: GitDiffLine['kind']): string {
  if (kind === 'add') return '+'
  if (kind === 'delete') return '-'
  return ' '
}

function hunkId(path: string, index: number): string {
  return `${scope.value}:${path}:${index}`
}

async function load(path: string, full: boolean): Promise<void> {
  const key = diffKey(path, full)
  const mine = (generation.get(key) ?? 0) + 1
  generation.set(key, mine)
  loading[path] = true
  try {
    if (!props.projectId) throw new ApiError(422, '无法读取该文件的差异')
    const result = await api.projectGitDiff(apiConfig(), props.projectId, path, scope.value, full)
    if (generation.get(key) !== mine) return
    diffs[key] = result.available ? result : { available: false, error: '无法读取该文件的差异' }
    if (full) sawFull[`${scope.value}:${path}`] = true
  } catch (error) {
    if (generation.get(key) !== mine) return
    const detail = error instanceof ApiError && typeof error.detail === 'string' ? error.detail : '无法读取该文件的差异'
    diffs[key] = { available: false, error: detail }
  } finally {
    if (generation.get(key) === mine) loading[path] = false
  }
}

function toggleFile(path: string): void {
  if (isOpen(path)) {
    open.value = open.value.filter((item) => item !== path)
    return
  }
  open.value = [...open.value, path]
  if (!currentDiff(path)) void load(path, false)
}

function toggleHunk(path: string, index: number): void {
  const key = hunkId(path, index)
  collapsed[key] = !collapsed[key]
}

async function refreshWorkspace(): Promise<void> {
  if (!props.projectId || refreshing.value) return
  refreshing.value = true
  for (const key of Object.keys(diffs)) delete diffs[key]
  for (const key of Object.keys(sawFull)) delete sawFull[key]
  generation.clear()
  try {
    await refreshGit(props.projectId)
  } finally {
    refreshing.value = false
  }
  const alive = new Set(shown.value.map((file) => file.path))
  open.value = open.value.filter((path) => alive.has(path))
  for (const path of open.value) void load(path, false)
}

watch(scope, () => {
  for (const path of open.value) {
    if (!currentDiff(path)) void load(path, false)
  }
})

watch(() => props.projectId, () => {
  open.value = []
  scope.value = 'all'
  generation.clear()
  for (const key of Object.keys(diffs)) delete diffs[key]
  for (const key of Object.keys(collapsed)) delete collapsed[key]
  for (const key of Object.keys(loading)) delete loading[key]
  for (const key of Object.keys(sawFull)) delete sawFull[key]
})

async function copyPaths(): Promise<void> {
  if (!shown.value.length) return
  try {
    await navigator.clipboard.writeText(shown.value.map((file) => file.path).join('\n'))
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
  <section
    role="complementary"
    aria-label="代码变更"
    class="panel"
    :style="maximized ? { width: '92%', maxWidth: 'none' } : undefined"
  >
    <header class="panel-head">
      <span class="panel-tabs">
        <button
          v-for="item in TABS"
          :key="item.key"
          class="tab"
          :class="{ 'is-active': tab === item.key }"
          type="button"
          @click="tab = item.key"
        >
          {{ item.label }}
        </button>
      </span>
      <span class="head-actions" style="margin-left: auto">
        <span class="anchor">
          <button class="ico" type="button" title="更多" @click="menu = !menu">
            <Icon name="dots" :size="14" :stroke-width="2" />
          </button>
          <Popover v-if="menu" align="down" end @close="menu = false">
            <button class="pop-item" type="button" :disabled="!shown.length" @click="copyPaths(); menu = false">
              <Icon name="copy" :size="13" />
              <span class="grow">{{ copied ? '已复制' : '复制变更路径' }}</span>
            </button>
            <div class="pop-sep" />
            <p class="pop-note">变更直接读取项目工作区的 Git 状态。</p>
          </Popover>
        </span>
        <button class="ico" type="button" title="最大化" @click="maximized = !maximized">
          <Icon name="maximize" :size="13" />
        </button>
        <button class="ico" type="button" title="关闭面板" @click="emit('close')">
          <Icon name="close" :size="13" />
        </button>
      </span>
    </header>

    <div class="panel-body">
      <div class="panel-tabs" style="padding: 0; border: 0; margin-bottom: 8px">
        <button
          v-for="item in SUBS"
          :key="item.key"
          class="tab"
          :class="{ 'is-active': sub === item.key }"
          type="button"
          @click="sub = item.key"
        >
          {{ item.label }}
        </button>
      </div>

      <template v-if="tab === 'changes' && sub === 'diff' && git?.available">
        <div class="diff-toolbar">
          <div class="diff-scopes" role="tablist" aria-label="差异范围">
            <button
              v-for="item in SCOPES"
              :key="item.key"
              class="tab"
              :class="{ 'is-active': scope === item.key }"
              type="button"
              role="tab"
              :aria-selected="scope === item.key"
              @click="scope = item.key"
            >
              {{ item.label }}
            </button>
          </div>
          <button class="ico" type="button" title="刷新工作区" :disabled="refreshing" @click="refreshWorkspace">
            <Icon name="refresh" :size="13" />
          </button>
        </div>

        <template v-if="shown.length">
          <div v-for="(group, groupIndex) in groups" :key="group.label || groupIndex" class="panel-group">
            <p v-if="group.label" class="panel-label">{{ group.label }}</p>
            <article v-for="file in group.files" :key="file.path">
              <button
                class="file-row"
                type="button"
                :aria-expanded="isOpen(file.path)"
                :title="file.old_path ? `${file.old_path} → ${file.path}` : file.path"
                @click="toggleFile(file.path)"
              >
                <Icon :name="isOpen(file.path) ? 'chevronDown' : 'chevronRight'" :size="13" />
                <span class="file-name">{{ shortPath(file.path, 60) }}</span>
                <span class="hint">{{ STATUS[file.status] || '修改' }}</span>
                <span class="diff-count">{{ countLabel(file) }}</span>
              </button>
              <div v-if="isOpen(file.path)" class="diff-view">
                <p v-if="loading[file.path] && !currentDiff(file.path)" class="diff-note">正在读取差异…</p>
                <template v-else-if="currentDiff(file.path)?.available">
                  <p v-if="currentDiff(file.path)?.binary" class="diff-note">二进制文件，不显示内容</p>
                  <p v-else-if="currentDiff(file.path)?.symlink" class="diff-note">
                    符号链接<span v-if="currentDiff(file.path)?.target"> → {{ currentDiff(file.path)?.target }}</span>，不跟随目标内容
                  </p>
                  <template v-else>
                    <p v-if="currentDiff(file.path)?.old_path" class="diff-note">
                      {{ currentDiff(file.path)?.old_path }} → {{ currentDiff(file.path)?.path }}
                    </p>
                    <p v-if="!currentDiff(file.path)?.hunks?.length" class="diff-note">此范围没有文本差异</p>
                    <section
                      v-for="(hunk, index) in currentDiff(file.path)?.hunks || []"
                      :key="`${file.path}:${index}`"
                      class="hunk"
                    >
                      <button
                        class="hunk-toggle"
                        type="button"
                        :aria-expanded="!collapsed[hunkId(file.path, index)]"
                        @click="toggleHunk(file.path, index)"
                      >
                        <span>{{ collapsed[hunkId(file.path, index)] ? '展开' : '折叠' }}</span>
                        <span class="hunk-header">{{ hunk.header }}</span>
                      </button>
                      <div v-show="!collapsed[hunkId(file.path, index)]" class="hunk-lines">
                        <div
                          v-for="(line, lineIndex) in hunk.lines"
                          :key="lineIndex"
                          class="diff-line"
                          :class="'is-' + line.kind"
                        >
                          <span class="diff-no">{{ lineNo(line) }}</span>
                          <span class="diff-mark">{{ mark(line.kind) }}</span>
                          <span class="diff-code">{{ line.text }}</span>
                        </div>
                      </div>
                    </section>
                    <button
                      v-if="currentDiff(file.path)?.truncated && !sawFull[`${scope}:${file.path}`]"
                      class="btn diff-full"
                      type="button"
                      @click="load(file.path, true)"
                    >
                      查看完整变更
                    </button>
                    <p v-else-if="currentDiff(file.path)?.truncated" class="diff-note">差异过长，已截断</p>
                  </template>
                </template>
                <p v-else class="diff-note">{{ currentDiff(file.path)?.error || '无法读取该文件的差异' }}</p>
              </div>
            </article>
          </div>
          <p class="panel-empty">
            来自 {{ title }} 工作区。整体差异比较工作区与 {{ git.head ? 'HEAD' : '空树' }}，已暂存比较索引，未暂存比较工作区与索引。未跟踪文件只在未暂存中列出。
          </p>
        </template>
        <p v-else class="panel-empty">{{ emptyText }}</p>
      </template>

      <p v-else class="panel-empty">{{ emptyText }}</p>
    </div>
  </section>
</template>

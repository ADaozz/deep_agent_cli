<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { api } from '../api/client'
import type { FilePreview, WorkspaceDirectory, WorkspaceFile } from '../api/types'
import { apiConfig } from '../store/settings'
import { workspaceFileVersions } from '../store/files'

const props = defineProps<{ projectId: string }>()
const directories = reactive<Record<string, WorkspaceDirectory>>({})
const loading = reactive<Record<string, boolean>>({})
const errors = reactive<Record<string, string>>({})
const expanded = ref<string[]>([])
const selected = ref('')
const preview = ref<FilePreview>()
const previewLoading = ref(false)
const previewError = ref('')
const refreshing = ref(false)
let epoch = 0
let sequence = 0
let previewSequence = 0
const requests = new Map<string, number>()

type TreeRow = { key: string; path: string; depth: number; kind: 'entry' | 'more' | 'error' | 'loading'; entry?: WorkspaceFile }
const rows = computed(() => {
  const result: TreeRow[] = []
  function append(path: string, depth: number): void {
    for (const entry of directories[path]?.entries || []) {
      result.push({ key: entry.path, path: entry.path, depth, kind: 'entry', entry })
      if (entry.kind === 'directory' && expanded.value.includes(entry.path)) append(entry.path, depth + 1)
    }
    if (errors[path]) result.push({ key: path + ':error', path, depth, kind: 'error' })
    else if (loading[path]) result.push({ key: path + ':loading', path, depth, kind: 'loading' })
    else if (directories[path]?.next_offset != null) result.push({ key: path + ':more', path, depth, kind: 'more' })
  }
  append('', 0)
  return result
})

async function loadDirectory(path: string, more = false): Promise<void> {
  const version = epoch
  const request = ++sequence
  requests.set(path, request)
  loading[path] = true
  delete errors[path]
  const offset = more ? directories[path]?.next_offset || 0 : 0
  try {
    const result = await api.workspaceFiles(apiConfig(), props.projectId, path, offset)
    if (version !== epoch || requests.get(path) !== request) return
    directories[path] = more ? { ...result, entries: [...(directories[path]?.entries || []), ...result.entries] } : result
  } catch (e) {
    if (version === epoch && requests.get(path) === request) errors[path] = e instanceof Error ? e.message : '无法读取目录'
  } finally {
    if (version === epoch && requests.get(path) === request) loading[path] = false
  }
}

async function loadPreview(path: string): Promise<void> {
  const version = epoch
  const request = ++previewSequence
  selected.value = path
  preview.value = undefined
  previewLoading.value = true
  previewError.value = ''
  try {
    const result = await api.previewFile(apiConfig(), props.projectId, path)
    if (version === epoch && request === previewSequence) preview.value = result
  } catch (e) {
    if (version === epoch && request === previewSequence) previewError.value = e instanceof Error ? e.message : '无法预览文件'
  } finally {
    if (version === epoch && request === previewSequence) previewLoading.value = false
  }
}

function select(entry: WorkspaceFile): void {
  if (entry.kind === 'directory') {
    if (expanded.value.includes(entry.path)) expanded.value = expanded.value.filter(path => path !== entry.path)
    else {
      expanded.value.push(entry.path)
      if (!directories[entry.path]) void loadDirectory(entry.path)
    }
  } else if (entry.kind !== 'other') void loadPreview(entry.path)
}

async function refresh(): Promise<void> {
  const version = ++epoch
  refreshing.value = true
  for (const key of Object.keys(loading)) delete loading[key]
  for (const key of Object.keys(errors)) delete errors[key]
  const paths = ['', ...expanded.value]
  await Promise.all([
    ...paths.map(path => loadDirectory(path)),
    selected.value ? loadPreview(selected.value) : Promise.resolve(),
  ])
  if (version === epoch) refreshing.value = false
}

watch(() => [props.projectId, workspaceFileVersions[props.projectId] || 0], (value, old) => {
  if (value[0] !== old?.[0]) {
    ++epoch
    ++previewSequence
    expanded.value = []
    selected.value = ''
    preview.value = undefined
    previewLoading.value = false
    previewError.value = ''
    for (const key of Object.keys(directories)) delete directories[key]
  }
  if (props.projectId) void refresh()
}, { immediate: true })

function sizeLabel(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}
</script>

<template>
  <div class="workspace-files">
    <div class="files-toolbar"><span>工作区文件</span><button class="ico" type="button" title="刷新文件树" :disabled="refreshing" @click="refresh"><Icon name="refresh" :size="13" /></button></div>
    <div class="workspace-tree" role="tree" aria-label="工作区文件树">
      <template v-for="row in rows" :key="row.key">
        <button v-if="row.kind === 'entry' && row.entry" class="tree-row" :class="{ 'is-selected': row.path === selected }" type="button" role="treeitem" :aria-level="row.depth + 1" :aria-expanded="row.entry.kind === 'directory' ? expanded.includes(row.path) : undefined" :aria-selected="row.path === selected" :disabled="row.entry.kind === 'other'" :title="row.path" :style="{ paddingLeft: 6 + row.depth * 16 + 'px' }" @click="select(row.entry)">
          <Icon v-if="row.entry.kind === 'directory'" :name="expanded.includes(row.path) ? 'chevronDown' : 'chevronRight'" :size="12" />
          <span v-else class="tree-spacer" />
          <Icon :name="row.entry.kind === 'directory' ? 'folder' : 'file'" :size="14" />
          <span class="file-name">{{ row.entry.name }}</span><span v-if="row.entry.kind === 'symlink'" class="hint">链接</span>
        </button>
        <button v-else-if="row.kind === 'more'" class="tree-more" type="button" :style="{ marginLeft: row.depth * 16 + 'px' }" @click="loadDirectory(row.path, true)">加载更多文件</button>
        <div v-else-if="row.kind === 'error'" class="tree-error" role="alert">{{ errors[row.path] }} <button class="btn" type="button" @click="loadDirectory(row.path)">重试</button></div>
        <p v-else-if="row.kind === 'loading'" class="diff-note" role="status">正在读取目录…</p>
      </template>
      <p v-if="!rows.length && !refreshing" class="panel-empty">此工作区为空</p>
    </div>
    <section v-if="selected" class="file-preview" aria-label="文件预览">
      <header class="file-preview-head"><span class="file-name" :title="selected">{{ selected }}</span><span class="hint">只读<span v-if="preview"> · {{ sizeLabel(preview.size) }}</span></span></header>
      <p v-if="previewLoading" class="diff-note" role="status">正在读取文件…</p>
      <p v-else-if="previewError" class="tree-error" role="alert">{{ previewError }}</p>
      <template v-else-if="preview">
        <pre v-if="preview.kind === 'text'" class="file-preview-code"><code>{{ preview.content }}</code></pre>
        <img v-else-if="preview.kind === 'image'" class="file-preview-image" :src="preview.data_url" :alt="selected" />
        <p v-else-if="preview.kind === 'symlink'" class="diff-note">符号链接 → {{ preview.target }}，不跟随目标内容</p>
        <p v-else class="diff-note">此文件为二进制文件或超出图片预览大小，无法显示内容。</p>
        <p v-if="preview.truncated && preview.kind === 'text'" class="diff-note">文件较大，仅显示前 256 KB。</p>
      </template>
    </section>
    <p v-else class="file-preview-placeholder">选择文件预览</p>
  </div>
</template>

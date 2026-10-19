<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { api } from '../api/client'
import type { GitCommit } from '../api/types'
import { apiConfig } from '../store/settings'
import { shortAge } from '../lib/timeline'

const props = defineProps<{ projectId: string; head?: string | null }>()
const commits = ref<GitCommit[]>([])
const cursor = ref<string | null>(null)
const loading = ref(false)
const error = ref('')
const copiedSha = ref('')
let generation = 0

function day(iso: string): string {
  return new Date(iso).toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
}
const groups = computed(() => {
  const result: { date: string; commits: GitCommit[] }[] = []
  for (const commit of commits.value) {
    const date = day(commit.committed_at)
    const previous = result[result.length - 1]
    if (previous?.date === date) previous.commits.push(commit)
    else result.push({ date, commits: [commit] })
  }
  return result
})

async function load(more = false): Promise<void> {
  if (!props.projectId || (more && (loading.value || !cursor.value))) return
  const version = more ? generation : ++generation
  loading.value = true
  error.value = ''
  if (!more) { commits.value = []; cursor.value = null }
  try {
    const result = await api.projectCommits(apiConfig(), props.projectId, more ? cursor.value || undefined : undefined)
    if (version !== generation) return
    if (!result.available) throw new Error('无法读取提交记录')
    commits.value = more ? [...commits.value, ...result.commits] : result.commits
    cursor.value = result.next_cursor
  } catch (e) {
    if (version === generation) error.value = e instanceof Error ? e.message : '无法读取提交记录'
  } finally {
    if (version === generation) loading.value = false
  }
}

watch(() => [props.projectId, props.head], () => { void load() }, { immediate: true })
defineExpose({ refresh: () => load(), loading })

async function copySha(sha: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(sha)
    copiedSha.value = sha
  } catch { error.value = '无法复制提交 SHA' }
}
</script>

<template>
  <div class="commit-history">
    <section v-for="(group, index) in groups" :key="group.date + index" class="commit-day">
      <h3 class="commit-day-heading"><Icon name="commit" :size="16" /><span>{{ group.date }}的提交</span></h3>
      <div class="commit-card">
        <article v-for="commit in group.commits" :key="commit.sha" class="commit-row" data-commit-row>
          <div class="commit-main">
            <a v-if="commit.url" class="commit-subject" :href="commit.url" :title="commit.message" target="_blank" rel="noreferrer">{{ commit.subject }}</a>
            <span v-else class="commit-subject" :title="commit.message">{{ commit.subject }}</span>
            <time class="commit-age" :datetime="commit.committed_at" :title="new Date(commit.committed_at).toLocaleString('zh-CN')">{{ shortAge(commit.committed_at) }}</time>
          </div>
          <div class="commit-details">
            <span class="commit-avatar" role="img" :aria-label="commit.author" :title="commit.author">{{ Array.from(commit.author)[0]?.toUpperCase() || '?' }}</span>
            <span class="commit-sha">{{ commit.sha.slice(0, 7) }}<button class="ico" type="button" :title="copiedSha === commit.sha ? '已复制 SHA' : '复制提交 SHA'" :aria-label="'复制提交 SHA ' + commit.sha.slice(0, 7)" @click="copySha(commit.sha)"><Icon :name="copiedSha === commit.sha ? 'check' : 'copy'" :size="12" /></button></span>
            <span v-if="commit.added != null && commit.removed != null" class="diff-count"><span class="diff-added">+{{ commit.added }}</span> <span class="diff-removed">-{{ commit.removed }}</span></span>
            <span v-if="commit.files != null" class="commit-files">{{ commit.files }} 个文件</span>
          </div>
        </article>
      </div>
    </section>
    <p v-if="loading" class="panel-empty" role="status">正在加载提交记录…</p>
    <div v-else-if="error" class="commit-error"><p role="alert">{{ error }}</p><button class="btn" type="button" @click="load(!!commits.length)">重试</button></div>
    <p v-else-if="!commits.length" class="panel-empty">暂无提交记录</p>
    <button v-else-if="cursor" class="btn commit-more" type="button" @click="load(true)">加载更多提交</button>
  </div>
</template>

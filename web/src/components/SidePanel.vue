<script setup lang="ts">
import { computed, ref } from 'vue'
import Icon from './Icon.vue'
import Popover from './Popover.vue'
import type { WorkspaceGit } from '../api/types'
import { shortPath } from '../lib/timeline'

const props = defineProps<{
  git?: WorkspaceGit
  title: string
  subtitle: string
}>()

const emit = defineEmits<{ close: [] }>()

const tab = ref<'changes' | 'desktop' | 'terminal' | 'files'>('changes')
const sub = ref<'diff' | 'review' | 'commits'>('diff')
const maximized = ref(false)
const menu = ref(false)
const copied = ref(false)

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

const files = computed(() => props.git?.available ? props.git.files || [] : [])

const emptyText = computed(() => {
  const labels = { desktop: '桌面', terminal: '终端', files: '文件' } as const
  if (tab.value !== 'changes') return `${labels[tab.value]}视图不由本控制台提供`
  return props.git?.available ? '工作区没有 Git 变更' : '工作区没有可用的 Git 仓库'
})

async function copyPaths(): Promise<void> {
  if (!files.value.length) return
  try {
    await navigator.clipboard.writeText(files.value.map((file) => file.path).join('\n'))
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
            <button class="pop-item" type="button" :disabled="!files.length" @click="copyPaths(); menu = false">
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

      <template v-if="tab === 'changes' && sub === 'diff' && files.length">
        <div class="panel-group">
          <details v-for="file in files" :key="file.path">
            <summary class="file-row">
              <Icon name="file" :size="13" />
              <span class="file-name" :title="file.path">{{ shortPath(file.path, 60) }}</span>
              <span class="hint">{{ file.status === 'untracked' ? '未跟踪' : file.status === 'deleted' ? '删除' : file.status === 'added' ? '新增' : '修改' }}</span>
            </summary><pre class="log-out">{{ file.binary ? '二进制文件' : file.added != null ? `+${file.added} / -${file.removed}` : '暂无行数统计' }}</pre>
          </details>
        </div>
        <p class="panel-empty">
          来自 {{ title }} 工作区的 Git 状态，与 HEAD 比较，包含暂存和未暂存改动。
        </p>
      </template>

      <p v-else class="panel-empty">{{ emptyText }}</p>
    </div>
  </section>
</template>

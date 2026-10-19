<script setup lang="ts">
import { computed } from 'vue'
import Icon from './Icon.vue'
import { app } from '../store/app'
import { workspaceGit } from '../store/git'
import { displayPath } from '../lib/paths'
import { projectLabel } from '../store/settings'

const props = defineProps<{ projectId: string; left: number; top: number }>()
const git = computed(() => workspaceGit[props.projectId])
const workspace = computed(() => app.projects.find(project => project.id === props.projectId))
const workspacePath = computed(() => displayPath(workspace.value?.path) || projectLabel(props.projectId))
const branch = computed(() => {
  if (!git.value?.available) return '无 Git 仓库'
  const name = git.value.branch === 'detached' ? '分离 HEAD · ' + git.value.head?.slice(0, 7) : git.value.branch
  return name + (git.value.upstream ? ' → ' + git.value.upstream : '')
})
</script>

<template>
  <div id="session-hover-card" role="tooltip" class="session-hover-card" data-sidebar-environment-hover-card="true" :style="{ left: left + 'px', top: top + 'px' }">
    <div class="session-hover-line" title="工作区当前变更">
      <Icon name="code" :size="13" />
      <span v-if="!git">正在读取变更…</span>
      <span v-else-if="!git.available">无 Git 变更信息</span>
      <span v-else class="session-hover-stats"><span>{{ git.changed || 0 }} 个文件</span><span v-if="git.added != null" class="diff-count"><span class="diff-added">+{{ git.added }}</span> <span class="diff-removed">-{{ git.removed }}</span></span></span>
    </div>
    <div class="session-hover-line is-workspace">
      <Icon name="branch" :size="13" />
      <div class="session-hover-workspace"><div class="session-hover-path" :title="workspacePath">{{ workspacePath }}</div><div class="session-hover-branch" :title="branch">{{ branch }}</div></div>
    </div>
  </div>
</template>

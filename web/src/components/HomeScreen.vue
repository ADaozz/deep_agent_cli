<script setup lang="ts">
/**
 * Home / New Chat: repository picker, the 640px prompt card, the two pills and
 * the "Recent agents" list. Everything is derived from `/v1/projects`,
 * `/v1/sessions` and the locally recorded runs.
 */
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import Icon from './Icon.vue'
import Popover from './Popover.vue'
import PromptComposer from './PromptComposer.vue'
import { app, openSession, sendFromHome, sessionRows, runBannerAction } from '../store/app'
import { longAge } from '../lib/timeline'
import { refreshGit, workspaceGit } from '../store/git'
import { models } from '../store/models'
import WorkspacePathDialog from './WorkspacePathDialog.vue'
import type { Workspace } from '../api/types'
import { isTerminal } from '../api/types'
import { api } from '../api/client'
import { recentWorkspacePaths, rememberWorkspace } from '../store/workspaces'
import { projectLabel, apiConfig } from '../store/settings'

const text = ref('')
const draftOpen = ref(false)
const branchOpen = ref(false)
const pathOpen = ref(false)
const branchBusy = ref(false)
const branchError = ref('')
const flashText = ref('')

const recent = computed(() => sessionRows.value.slice(0, 4))

const selectedWorkspace = computed(() => app.projects.find(project => project.id === app.draftProjectId) || app.projects[0])
const workspaceHistory = computed(() => recentWorkspacePaths.map(path => app.projects.find(project => project.path === path)).filter((project): project is Workspace => !!project).slice(0, 4))
const git = computed(() => workspaceGit[selectedWorkspace.value?.id || ''])
const workspaceBusy = computed(() => branchBusy.value || app.busy.submit || sessionRows.value.some(row => row.projectId === selectedWorkspace.value?.id && row.status && !isTerminal(row.status)))

function flash(message: string): void {
  flashText.value = message
  window.setTimeout(() => {
    if (flashText.value === message) flashText.value = ''
  }, 4000)
}

function pickProject(projectId: string): void {
  const workspace = app.projects.find(project => project.id === projectId)
  if (!workspace) return
  app.draftProjectId = projectId
  rememberWorkspace(workspace.path)
  draftOpen.value = false
  branchOpen.value = false
  branchError.value = ''
  void refreshGit(projectId)
}
function selectedPath(workspace: Workspace): void {
  if (!app.projects.some(project => project.id === workspace.id)) app.projects.push(workspace)
  pickProject(workspace.id)
  pathOpen.value = false
}
async function switchBranch(branch: string): Promise<void> {
  if (!selectedWorkspace.value || workspaceBusy.value) return
  const projectId = selectedWorkspace.value.id
  branchBusy.value = true; branchError.value = ''
  try { workspaceGit[projectId] = await api.switchBranch(apiConfig(), projectId, branch); branchOpen.value = false }
  catch (e) { branchError.value = e instanceof Error ? e.message : '无法切换分支' }
  finally { branchBusy.value = false }
}

async function submit(value: string): Promise<void> {
  const ok = await sendFromHome(value)
  if (ok) text.value = ''
}

function refreshProjects(): void {
  for (const project of app.projects) void refreshGit(project.id)
}
watch(() => app.projects.map(project => project.id).join(','), refreshProjects, { immediate: true })
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(refreshProjects, 5000) })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
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
          <button class="pick workspace-pick" type="button" aria-label="选择 workspace" :title="selectedWorkspace?.path" :disabled="branchBusy" @click="draftOpen = !draftOpen; branchOpen = false">
            <span class="workspace-label">{{ selectedWorkspace?.path || '选择 workspace' }}</span><Icon name="chevronDown" :size="11" />
          </button>
          <Popover v-if="draftOpen" align="down" @close="draftOpen = false">
            <div class="pop-head">最近 workspace</div>
            <button v-for="workspace in workspaceHistory" :key="workspace.id" class="pop-item" type="button" :title="workspace.path" @click="pickProject(workspace.id)">
              <Icon name="folder" :size="13" /><span class="grow workspace-history-label"><span>{{ workspace.path.split('/').pop() || '/' }}</span><small class="workspace-history-path">{{ workspace.path }}</small></span><Icon v-if="selectedWorkspace?.id === workspace.id" name="check" :size="13" />
            </button>
            <div v-if="workspaceHistory.length" class="pop-sep" />
            <button class="pop-item" type="button" @click="draftOpen = false; pathOpen = true"><Icon name="folder" :size="13" /><span class="grow">选择路径…</span></button>
          </Popover>
        </span>
        <span v-if="git?.available" class="anchor">
          <button class="pick" type="button" aria-label="选择 Git 分支" :disabled="workspaceBusy" @click="branchOpen = !branchOpen; draftOpen = false; branchError = ''; refreshGit(selectedWorkspace!.id)">
            <Icon name="branch" :size="14" /><span>{{ git.branch }}</span><Icon name="chevronDown" :size="10" />
          </button>
          <Popover v-if="branchOpen" align="down" end @close="branchOpen = false">
            <div class="pop-head">Git 分支</div>
            <button v-for="branch in git.branches || []" :key="branch" class="pop-item" type="button" :disabled="workspaceBusy" @click="switchBranch(branch)"><span class="grow">{{ branch }}</span><Icon v-if="branch === git.branch" name="check" :size="13" /></button>
            <p v-if="branchError" class="pop-note model-error" role="alert">{{ branchError }}</p>
          </Popover>
        </span>
      </div>
      <WorkspacePathDialog v-if="pathOpen" :initial-path="selectedWorkspace?.path" @close="pathOpen = false" @selected="selectedPath" />

      <PromptComposer
        v-model="text"
        placeholder="让 Deep Agent 构建、修 bug、探索"
        :running="false"
        :busy="app.busy.submit"
        :disabled="!app.conn.authorized || branchBusy"
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
              <span>{{ row.label }}</span>
              <span v-if="workspaceGit[row.projectId]?.available" class="git-diff" aria-label="Git diff">
                <template v-if="workspaceGit[row.projectId].added != null">+{{ workspaceGit[row.projectId].added }} / -{{ workspaceGit[row.projectId].removed }}</template>
                <template v-else>{{ workspaceGit[row.projectId].changed }} 个变更路径</template>
              </span>
            </span>
            <span v-if="workspaceGit[row.projectId]?.available && workspaceGit[row.projectId].branch" class="mini-pill" aria-label="Git 分支">
              <Icon name="branch" :size="10" />{{ workspaceGit[row.projectId].branch }}
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
              <span v-if="workspaceGit[row.projectId]?.available && workspaceGit[row.projectId].merged === true" class="git-merged" aria-label="Git 合并状态">HEAD 已合入 {{ workspaceGit[row.projectId].base_branch }}</span>
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

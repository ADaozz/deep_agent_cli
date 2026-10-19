<script setup lang="ts">
/**
 * Home / New Chat: repository picker, the 640px prompt card, the two pills and
 * the "Recent agents" list. Everything is derived from `/v1/projects`,
 * `/v1/sessions` and the locally recorded runs.
 */
import { computed, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { displayPath } from '../lib/paths'
import Popover from './Popover.vue'
import PromptComposer from './PromptComposer.vue'
import { useDraft, readDraft, draftPrompt, clearSentDraft } from '../store/drafts'
import { app, openSession, sendFromHome, sessionRows, runBannerAction } from '../store/app'
import { longAge } from '../lib/timeline'
import { refreshGit, workspaceGit } from '../store/git'
import { models } from '../store/models'
import WorkspacePathDialog from './WorkspacePathDialog.vue'
import type { Workspace } from '../api/types'
import { recentWorkspacePaths, rememberWorkspace } from '../store/workspaces'
import { projectLabel } from '../store/settings'

const draftKey = computed(() => 'home:' + (selectedWorkspace.value?.path || ''))
const { text, paths, skills } = useDraft(draftKey)
const draftOpen = ref(false)
const branchOpen = ref(false)
const pathOpen = ref(false)

const recent = computed(() => sessionRows.value.slice(0, 4))

const selectedWorkspace = computed(() => app.projects.find(project => project.id === app.draftProjectId) || app.projects[0])
const workspaceHistory = computed(() => recentWorkspacePaths.map(path => app.projects.find(project => project.path === path)).filter((project): project is Workspace => !!project).slice(0, 4))
const git = computed(() => workspaceGit[selectedWorkspace.value?.id || ''])
const branchRows = computed(() => {
  const info = git.value
  if (!info) return []
  const upstreams = info.branch_upstreams || {}
  const linked = new Set(Object.values(upstreams))
  return [
    ...(info.branches || []).filter(branch => !upstreams[branch]).map(branch => ({
      key: 'local:' + branch,
      label: branch,
      current: branch === info.branch,
    })),
    ...(info.remote_branches || []).filter(branch => !linked.has(branch)).map(branch => ({
      key: 'remote:' + branch, label: branch, current: false,
    })),
    ...(info.branches || []).filter(branch => upstreams[branch]).map(branch => ({
      key: 'local:' + branch,
      label: branch + ' → ' + upstreams[branch],
      current: branch === info.branch,
    })),
  ]
})

function pickProject(projectId: string): void {
  const workspace = app.projects.find(project => project.id === projectId)
  if (!workspace) return
  app.draftProjectId = projectId
  rememberWorkspace(workspace.path)
  draftOpen.value = false
  branchOpen.value = false
  void refreshGit(projectId)
}
function selectedPath(workspace: Workspace): void {
  if (!app.projects.some(project => project.id === workspace.id)) app.projects.push(workspace)
  pickProject(workspace.id)
  pathOpen.value = false
}
async function submit(): Promise<void> {
  const key = draftKey.value
  const snapshot = JSON.stringify(readDraft(key))
  const ok = await sendFromHome(draftPrompt(readDraft(key), false), readDraft(key).paths.map(item => item.path))
  if (ok) {
    clearSentDraft(key, snapshot)
  }
}

function refreshProjects(): void {
  for (const project of app.projects) void refreshGit(project.id)
}
watch(() => app.projects.map(project => project.id).join(','), refreshProjects, { immediate: true })
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
          <button class="pick workspace-pick" type="button" aria-label="选择 workspace" :title="displayPath(selectedWorkspace?.path)" @click="draftOpen = !draftOpen; branchOpen = false">
            <span class="workspace-label">{{ displayPath(selectedWorkspace?.path) || '选择 workspace' }}</span><Icon name="chevronDown" :size="11" />
          </button>
          <Popover v-if="draftOpen" align="down" @close="draftOpen = false">
            <div class="pop-head">最近 workspace</div>
            <button v-for="workspace in workspaceHistory" :key="workspace.id" class="pop-item" type="button" :title="displayPath(workspace.path)" @click="pickProject(workspace.id)">
              <Icon name="folder" :size="13" /><span class="grow workspace-history-label"><span>{{ workspace.path.split('/').pop() || '/' }}</span><small class="workspace-history-path">{{ displayPath(workspace.path) }}</small></span><Icon v-if="selectedWorkspace?.id === workspace.id" name="check" :size="13" />
            </button>
            <div v-if="workspaceHistory.length" class="pop-sep" />
            <button class="pop-item" type="button" @click="draftOpen = false; pathOpen = true"><Icon name="folder" :size="13" /><span class="grow">选择路径…</span></button>
          </Popover>
        </span>
        <span v-if="git?.available" class="anchor">
          <button class="pick" type="button" aria-label="查看 Git 分支" :aria-expanded="branchOpen" @click="branchOpen = !branchOpen; draftOpen = false; refreshGit(selectedWorkspace!.id)">
            <Icon name="branch" :size="14" /><span>{{ git.branch }}</span><Icon name="chevronDown" :size="10" />
          </button>
          <Popover v-if="branchOpen" align="down" class="branch-menu" @close="branchOpen = false">
            <div role="list" aria-label="全部 Git 分支">
              <div v-for="branch in branchRows" :key="branch.key" role="listitem" class="branch-value" :aria-current="branch.current ? 'true' : undefined">
                <span class="grow">{{ branch.label }}</span><Icon v-if="branch.current" name="check" :size="13" />
              </div>
            </div>
          </Popover>
        </span>
      </div>
      <WorkspacePathDialog v-if="pathOpen" :initial-path="selectedWorkspace?.path" @close="pathOpen = false" @selected="selectedPath" />

      <PromptComposer
        v-model="text"
        v-model:paths="paths"
        v-model:skills="skills"
        :draft-key="draftKey"
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
          @click="text = '请审查当前项目的安全风险，给出有代码位置依据的发现和修复建议。'"
        >
          运行安全审计
        </button>
      </div>


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
                <template v-if="workspaceGit[row.projectId].added != null"><span class="git-added">+{{ workspaceGit[row.projectId].added }}</span> / <span class="git-removed">-{{ workspaceGit[row.projectId].removed }}</span></template>
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

import { imageModelRequiredMessage, notifyFileReference } from './fileReferences'
import { clearDrafts } from './drafts'
import { homePath } from '../lib/paths'
import { refreshGit } from './git'
import { invalidateWorkspaceFiles } from './files'
import { readThreadLocation, writeThreadLocation } from './threadLocation'
import { applyDraftPermission } from './capabilities'
import { computed, reactive } from 'vue'
import { recentWorkspacePaths, rememberWorkspace } from './workspaces'
import { applyDraftModel, models } from './models'
import {
  ApiError,
  NetworkError,
  api,
  describeDetail,
  getHealth,
  streamEvents,
} from '../api/client'
import type { ApprovalDecision, Run, RunStatus, Session, SseMessage, Workspace, SavedTranscriptBlock } from '../api/types'
import { isTerminal } from '../api/types'
import {
  applySseMessage,
  finalizeRecord,
  interactionOf,
  type InteractionModel,
  type RunRecord,
  type SessionRecord,
} from '../lib/runView'
import { STATUS_UI } from '../lib/timeline'
import {
  makeRunRecord,
  type RecordMap,
} from './records'
import { apiConfig } from './settings'

/** Which of the two screens is visible: the new-chat home or an agent thread. */
export type AppView = 'home' | 'thread'

export interface Banner {
  kind: 'error' | 'warn' | 'info'
  text: string
  hint?: string
  actions: { key: BannerAction; label: string }[]
}

export type BannerAction = 'open-settings' | 'refresh' | 'dismiss'

export const app = reactive({
  projects: [] as Workspace[],
  projectFilter: 'all',
  sessions: [] as Session[],
  loading: { projects: false, sessions: false },
  sessionId: '',
  view: 'home' as AppView,
  /** Repository picked for the next new chat on the home screen. */
  draftProjectId: '',
  records: {} as RecordMap,
  conn: { health: false, authorized: false, error: '', checkedAt: '' },
  banner: null as Banner | null,
  busy: { submit: false, resume: false, cancel: false, session: false },
  settingsOpen: false,
  history: {} as Record<string, { loading: boolean; cursor: string | null }>,
  transcripts: {} as Record<string, { blocks: SavedTranscriptBlock[]; cursor: string | null; loading: boolean; loaded: boolean; error: string }>,
})

const streamHandles = new Map<string, AbortController>()

export const currentRecord = computed<SessionRecord | null>(
  () => app.records[app.sessionId] ?? null,
)

export const currentRuns = computed<RunRecord[]>(() => currentRecord.value?.runs ?? [])

export const latestRecord = computed<RunRecord | null>(() => {
  const runs = currentRuns.value
  return runs.length ? runs[runs.length - 1] : null
})

export const currentInteraction = computed<InteractionModel | null>(() =>
  interactionOf(latestRecord.value),
)

export const currentRunActive = computed<boolean>(() => {
  const latest = latestRecord.value
  return !!latest && !isTerminal(latest.run.status)
})

/* ------------------------------------------------------- sidebar / list rows */

export interface SessionRow {
  id: string
  title: string
  projectId: string
  createdAt: string
  updatedAt: string
  status: RunStatus | null
  label: string
  tone: 'ok' | 'run' | 'wait' | 'bad' | 'idle'
  active: boolean
  /** Newest run of this session, used for the "Worked for" line. */
  latest: RunRecord | null
}

function rowTitle(record: SessionRecord | undefined): string {
  const title = (record?.title ?? '').replace(/\s+/g, ' ').trim()
  if (!title) return `会话 ${record?.session.id.slice(0, 8) || ''}`
  return title.length > 72 ? `${title.slice(0, 72)}…` : title
}

function makeRow(session: Session, record: SessionRecord | undefined): SessionRow {
  const latest = record && record.runs.length ? record.runs[record.runs.length - 1] : null
  const status = latest?.run.status ?? null
  const ui = status ? STATUS_UI[status] : null
  return {
    id: session.id,
    title: rowTitle(record),
    projectId: session.project_id,
    createdAt: session.created_at,
    updatedAt: latest?.run.updated_at || session.created_at,
    status,
    label: ui?.label ?? '空闲',
    tone: ui?.tone ?? 'idle',
    active: app.sessionId === session.id,
    latest,
  }
}

/**
 * Sidebar rows and the home "recent agents" list both read this: the session
 * list from `/v1/sessions` joined with server run summaries and cached prompts.
 */
export const sessionRows = computed<SessionRow[]>(() => {
  const rows: SessionRow[] = []
  const seen = new Set<string>()
  for (const session of app.sessions) {
    rows.push(makeRow(session, app.records[session.id]))
    seen.add(session.id)
  }
  for (const [id, record] of Object.entries(app.records)) {
    if (seen.has(id)) continue
    rows.push(makeRow(record.session, record))
  }
  return rows.sort((a, b) => b.createdAt.localeCompare(a.createdAt))
})

export const currentRow = computed<SessionRow | null>(() => {
  const id = app.sessionId
  if (!id) return null
  return sessionRows.value.find((row) => row.id === id) ?? null
})

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      resolve()
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

export function setBanner(
  kind: Banner['kind'],
  text: string,
  hint?: string,
  actions: Banner['actions'] = [{ key: 'dismiss', label: '关闭' }],
): void {
  app.banner = { kind, text, hint, actions }
}

export function clearBanner(): void {
  app.banner = null
}

export function runBannerAction(action: BannerAction): void {
  switch (action) {
    case 'open-settings':
      app.settingsOpen = true
      break
    case 'refresh':
      void connect()
      break
    default:
      clearBanner()
      break
  }
}

function explain(
  error: unknown,
  context: string,
): { kind: Banner['kind']; text: string; hint?: string; actions: Banner['actions'] } {
  if (error instanceof NetworkError) {
    return {
      kind: 'error',
      text: `${context}失败：无法连接服务。`,
      hint: '请检查本地 HTTP 服务与同源网关是否已启动。',
      actions: [
        { key: 'refresh', label: '重新连接' },
        { key: 'open-settings', label: '连接设置' },
        { key: 'dismiss', label: '关闭' },
      ],
    }
  }
  if (error instanceof ApiError) {
    const detail = describeDetail(error.detail)
    switch (error.status) {
      case 401:
        return {
          kind: 'error',
          text: '服务连接认证失败（401）。',
          hint: '请检查服务和网关配置中的令牌是否一致。',
          actions: [{ key: 'open-settings', label: '连接设置' }, { key: 'dismiss', label: '关闭' }],
        }
      case 404:
        return {
          kind: 'error',
          text: `${context}失败：未找到（404）。`,
          hint: '项目可能已从服务配置中移除，或会话/运行 ID 已失效。',
          actions: [{ key: 'refresh', label: '重新加载' }, { key: 'dismiss', label: '关闭' }],
        }
      case 409:
        return {
          kind: 'warn',
          text: `${context}冲突（409）：${detail || '状态已变化'}`,
          hint: detail.includes('另一个 Deep Agent')
            ? '请先结束另一端再提交。已保留你输入的文本。'
            : '每个项目只允许一个未结束的运行，且交互会过期。已保留你输入的文本。',
          actions: [{ key: 'refresh', label: '同步' }, { key: 'dismiss', label: '关闭' }],
        }
      case 422:
        return {
          kind: 'warn',
          text: `${context}被拒绝（422）。`,
          hint: detail,
          actions: [{ key: 'dismiss', label: '关闭' }],
        }
      case 429:
        return {
          kind: 'warn',
          text: '服务运行容量已满（429）。',
          hint: '未入队任何任务——等其它项目结束后再重试。',
          actions: [{ key: 'dismiss', label: '关闭' }],
        }
      case 503:
        return {
          kind: 'warn',
          text: '服务正在关闭（503）。',
          hint: '请稍后重试，并先检查已有运行以免重复。',
          actions: [{ key: 'refresh', label: '同步' }, { key: 'dismiss', label: '关闭' }],
        }
      default:
        return {
          kind: 'error',
          text: `${context}失败（HTTP ${error.status}）。`,
          hint: detail,
          actions: [{ key: 'refresh', label: '同步' }, { key: 'dismiss', label: '关闭' }],
        }
    }
  }
  return {
    kind: 'error',
    text: `${context}失败：${error instanceof Error ? error.message : String(error)}`,
    actions: [{ key: 'dismiss', label: '关闭' }],
  }
}

function showError(error: unknown, context: string): void {
  if (error instanceof ApiError && error.status === 422 && error.detail === imageModelRequiredMessage) {
    notifyFileReference(imageModelRequiredMessage)
    return
  }
  const info = explain(error, context)
  setBanner(info.kind, info.text, info.hint, info.actions)
}

// ---------------------------------------------------------------- connections

let connecting: Promise<void> | null = null

export function connect(): Promise<void> {
  if (!connecting) {
    connecting = connectOnce().finally(() => {
      connecting = null
    })
  }
  return connecting
}

async function connectOnce(): Promise<void> {
  clearBanner()
  app.loading.projects = true
  try {
    try {
      app.conn.health = await getHealth(apiConfig().baseUrl)
    } catch {
      app.conn.health = false
      throw new NetworkError('health check failed')
    }
    const result = await api.workspaces(apiConfig())
    homePath.value = result.home_path || ''
    app.projects = result.workspaces ?? []
    const selected = app.projects.find(project => project.id === app.draftProjectId)
      || app.projects.find(project => project.path === recentWorkspacePaths[0]) || app.projects[0]
    app.draftProjectId = selected?.id || ''
    if (selected) rememberWorkspace(selected.path)
    app.conn.authorized = true
    app.conn.error = ''
    app.conn.checkedAt = new Date().toISOString()
    if (app.projectFilter !== 'all' && !app.projects.some((p) => p.id === app.projectFilter)) {
      app.projectFilter = app.projects[0]?.id ?? 'all'
    }
    await loadSessions()
    restoreSelection()
    await discoverActiveRuns()
    resumeStreams()
  } catch (error) {
    app.conn.authorized = false
    app.conn.error = error instanceof Error ? error.message : String(error)
    showError(error, '连接')
  } finally {
    app.loading.projects = false
  }
}

export async function loadSessions(): Promise<void> {
  app.loading.sessions = true
  try {
    const projectId = app.projectFilter === 'all' ? undefined : app.projectFilter
    const result = await api.listSessions(apiConfig(), projectId)
    app.sessions = (result.sessions ?? []).slice().sort((a, b) => b.created_at.localeCompare(a.created_at))
    for (const session of app.sessions) {
      const existing = app.records[session.id]
      if (existing) {
        existing.session = session
        if (!existing.title && session.title) existing.title = session.title
      } else if (session.title) {
        ensureRecord(session).title = session.title
      }
    }
  } catch (error) {
    showError(error, '加载会话')
  } finally {
    app.loading.sessions = false
  }
}

export function setProjectFilter(projectId: string): void {
  app.projectFilter = projectId
  void loadSessions()
}

/** Re-open the session the user was looking at before reloading the page. */
function restoreSelection(): void {
  if (app.sessionId) return
  const saved = readThreadLocation()
  if (saved && (app.records[saved] || app.sessions.some((item) => item.id === saved))) {
    selectSession(saved)
    // Reopening the page lands on the thread the user was reading, like the real page.
    app.view = 'thread'
    return
  }
  const recent = Object.values(app.records)
    .sort((a, b) => b.session.created_at.localeCompare(a.session.created_at))[0]
  if (recent) {
    selectSession(recent.session.id)
    app.view = 'thread'
  }
}

/* ---------------------------------------------------------- screen switching */

/** Back to the new-chat screen; the previous session stays in the sidebar. */
export function goHome(): void {
  app.sessionId = ''
  app.view = 'home'
  writeThreadLocation('')
}

export function openSession(sessionId: string): void {
  selectSession(sessionId)
  app.view = 'thread'
}

/**
 * Home composer: create a session for the picked repository, then start the
 * first run in it. The session is only shown as "active" once the run exists,
 * so a rejected submit keeps the user on the home screen with their text.
 */
async function discardEmptySession(sessionId: string): Promise<void> {
  app.sessions = app.sessions.filter((item) => item.id !== sessionId)
  delete app.records[sessionId]
  delete app.history[sessionId]
  delete app.transcripts[sessionId]
  if (app.sessionId === sessionId) {
    app.sessionId = ''
    app.view = 'home'
    writeThreadLocation('')
  }
  try {
    await api.deleteSession(apiConfig(), sessionId)
  } catch {
    await loadSessions().catch(() => undefined)
  }
}

export async function sendFromHome(text: string, filePaths: string[] = []): Promise<boolean> {
  const value = text.trim()
  if (!value && !filePaths.length) return false
  if (app.busy.submit) return false
  app.busy.submit = true
  const located = readThreadLocation()
  if (located && located !== app.sessionId) {
    await connect()
    const known = app.sessions.some((item) => item.id === located) || Boolean(app.records[located])
    if (known) {
      selectSession(located)
      app.view = 'thread'
      app.busy.submit = false
      return submitText(value, filePaths)
    }
  }
  const projectId = app.draftProjectId || app.projects[0]?.id || ''
  if (!projectId) {
    setBanner(
      'warn',
      '没有可用的仓库。',
      '服务配置里没有任何项目。请在服务 YAML 中添加后重新连接。',
    )
    app.busy.submit = false
    return false
  }
  clearBanner()
  let sessionId = ''
  try {
    const session = await api.createSession(apiConfig(), projectId)
    sessionId = session.id
    ensureRecord(session)
    if (!app.sessions.some((item) => item.id === session.id)) {
      app.sessions = [session, ...app.sessions]
    }

    selectSession(session.id)
    await applyDraftModel(projectId, session.id)
    await applyDraftPermission(projectId, session.id)
    const run = await api.createRun(apiConfig(), session.id, { text: value, ...(filePaths.length ? { file_paths: filePaths } : {}) })
    const runRecord = makeRunRecord(run, [value, ...filePaths].filter(Boolean).join('\n'), false)
    attachRun(app.records[session.id], runRecord)
    streamRun(runRecord)
    app.view = 'thread'
    return true
  } catch (error) {
    showError(error, '启动 Agent')
    const record = sessionId ? app.records[sessionId] : undefined
    if (sessionId && (!record || record.runs.length === 0)) {
      await discardEmptySession(sessionId)
    }
    return false
  } finally {
    app.busy.submit = false
  }
}

export async function createSession(explicitProjectId?: string): Promise<void> {
  const projectId =
    explicitProjectId ||
    (app.projectFilter !== 'all' ? app.projectFilter : (app.projects[0]?.id ?? ''))
  if (!projectId) {
    setBanner('warn', '没有可用仓库。', '服务配置里没有项目，或列表尚未加载完成。')
    return
  }
  app.busy.session = true
  try {
    const session = await api.createSession(apiConfig(), projectId)
    if (app.sessionId && app.sessionId !== session.id) {
      // keep any running stream of the previous session alive
    }
    const record = ensureRecord(session)
    if (!app.sessions.some((item) => item.id === session.id)) {
      app.sessions = [session, ...app.sessions]
    }
    selectSession(session.id)
    record.draft = ''
    // Keep the list filter in sync so a refresh does not hide the new session.
    if (app.projectFilter !== 'all' && app.projectFilter !== projectId) setProjectFilter(projectId)
  } catch (error) {
    showError(error, '创建会话')
  } finally {
    app.busy.session = false
  }
}

export function selectSession(sessionId: string): void {
  app.sessionId = sessionId
  writeThreadLocation(sessionId)
  let record = app.records[sessionId]
  if (!record) {
    const session = app.sessions.find((item) => item.id === sessionId)
    if (session) record = ensureRecord(session)
  }
  if (record) void loadSessionHistory(sessionId).catch((error) => showError(error, '加载历史'))
}

export function ensureRecord(session: Session): SessionRecord {
  const existing = app.records[session.id]
  if (existing) {
    existing.session = session
    return existing
  }
  app.records[session.id] = { session, title: '', runs: [], draft: '' }
  // Return the reactive proxy, never the plain object that was just stored.
  return app.records[session.id]
}

export function attachRun(record: SessionRecord, runRecord: RunRecord): void {
  const target = app.records[record.session.id] ?? record
  const index = target.runs.findIndex((item) => item.id === runRecord.id)
  if (index >= 0) target.runs[index] = runRecord
  else target.runs.push(runRecord)
  target.runs.sort((a, b) => a.run.created_at.localeCompare(b.run.created_at) || a.id.localeCompare(b.id))
  if (!target.title && runRecord.prompt && !runRecord.prompt.startsWith('原始指令不可用')) {
    target.title = runRecord.prompt.replace(/\s+/g, ' ').trim().slice(0, 80)
  }
}

/** Fetch server summaries and replay logs even for completed runs. */
export async function loadSessionHistory(sessionId: string, more = false): Promise<void> {
  const record = app.records[sessionId]
  if (!record || app.history[sessionId]?.loading) return
  const previous = app.history[sessionId]
  const cursor = more ? previous?.cursor ?? undefined : undefined
  if (more && !cursor) return
  app.history[sessionId] = { loading: true, cursor: previous?.cursor ?? null }
  try {
    const page = await api.sessionRuns(apiConfig(), sessionId, cursor)
    if (app.records[sessionId] !== record) return
    const snapshots = await Promise.all(page.runs.map((run) => api.getRun(apiConfig(), run.id)))
    for (const snapshot of snapshots) {
      const existing = record.runs.find((run) => run.id === snapshot.id)
      if (existing) {
        applyRunSnapshot(existing, snapshot)
        streamRun(existing)
      } else {
        const saved = typeof snapshot.prompt === 'string' ? snapshot.prompt.trim() : ''
        const restored = makeRunRecord(snapshot, saved || '原始指令不可用（服务未持久化提交文本）', false)
        attachRun(record, restored)
        streamRun(restored)
      }
    }
    app.history[sessionId].cursor = page.next_cursor
    if (page.next_cursor == null) await loadSessionTranscript(sessionId)
  } finally {
    if (app.history[sessionId]) app.history[sessionId].loading = false
  }
}

export async function loadSessionTranscript(sessionId: string, more = false): Promise<void> {
  const record = app.records[sessionId]
  const previous = app.transcripts[sessionId]
  if (!record || previous?.loading || (more && !previous?.cursor)) return
  const state = { blocks: previous?.blocks || [], cursor: previous?.cursor || null,
    loading: true, loaded: previous?.loaded || false, error: '' }
  app.transcripts[sessionId] = state
  try {
    const page = await api.sessionTranscript(apiConfig(), sessionId, more ? state.cursor || undefined : undefined)
    if (app.records[sessionId] !== record) return
    app.transcripts[sessionId] = { blocks: more ? [...(page.blocks || []), ...state.blocks] : page.blocks || [],
      cursor: page.next_cursor || null, loading: false, loaded: true, error: '' }
  } catch (error) {
    if (app.records[sessionId] === record) app.transcripts[sessionId] = { ...state, loading: false,
      error: error instanceof Error ? error.message : '无法加载保存的历史' }
  }
}

async function discoverActiveRuns(): Promise<void> {
  const active = await Promise.all(app.projects.map((project) => api.activeRun(apiConfig(), project.id)))
  for (const result of active) {
    if (!result.run) continue
    const session = app.sessions.find((session) => session.id === result.run?.session_id)
    if (!session) continue
    ensureRecord(session)
    await loadSessionHistory(session.id)
    if (!app.sessionId) {
      app.sessionId = session.id
      app.view = 'thread'
      writeThreadLocation(session.id)
    }
  }
}

// ---------------------------------------------------------------- run actions

export async function submitText(text: string, filePaths: string[] = []): Promise<boolean> {
  const record = currentRecord.value
  if (!record || models.changing[record.session.id]) return false
  const value = text.trim()
  if (!value && !filePaths.length) return false
  if (app.busy.submit) return false
  app.busy.submit = true
  clearBanner()
  try {
    const run = await api.createRun(apiConfig(), record.session.id, { text: value, ...(filePaths.length ? { file_paths: filePaths } : {}) })
    const runRecord = makeRunRecord(run, [value, ...filePaths].filter(Boolean).join('\n'), false)
    attachRun(record, runRecord)
    streamRun(runRecord)
    return true
  } catch (error) {
    // Keep the draft: a 409/429/503 promise means the text was NOT accepted.
    showError(error, '发送任务')
    return false
  } finally {
    app.busy.submit = false
  }
}

export async function resumeSession(): Promise<void> {
  const record = currentRecord.value
  if (!record) return
  if (app.busy.resume || currentRunActive.value) return
  app.busy.resume = true
  clearBanner()
  try {
    const run = await api.createRun(apiConfig(), record.session.id, { resume: true })
    const runRecord = makeRunRecord(run, '恢复被中断的会话', true)
    attachRun(record, runRecord)
    streamRun(runRecord)
  } catch (error) {
    showError(error, '恢复会话')
  } finally {
    app.busy.resume = false
  }
}

function findRunRecord(runId: string): RunRecord | null {
  for (const record of Object.values(app.records)) {
    const found = record.runs.find((item) => item.id === runId)
    if (found) return found
  }
  return null
}

/**
 * Run records are often created as plain objects and then stored in the
 * reactive `app.records` map. Mutating the original plain object would bypass
 * Vue's notifications, so every writer resolves the reactive instance first.
 */
function canonicalRun(runRecord: RunRecord): RunRecord {
  return findRunRecord(runRecord.id) ?? runRecord
}

const gitRefreshedRuns = new Set<string>()

function refreshGitAfterRun(run: Run): void {
  if (!isTerminal(run.status) || gitRefreshedRuns.has(run.id)) return
  gitRefreshedRuns.add(run.id)
  invalidateWorkspaceFiles(run.project_id)
  void refreshGit(run.project_id)
}

export function applyRunSnapshot(runRecord: RunRecord, snapshot: Run): void {
  const target = canonicalRun(runRecord)
  // Never let an older snapshot (e.g. a slow poll) roll the UI back.
  if (snapshot.updated_at && target.run?.updated_at && snapshot.updated_at < target.run.updated_at) {
    return
  }
  const previousInterruptId = target.run.interrupt_id
  const wasActive = !isTerminal(target.run.status)
  target.run = snapshot
  if (snapshot.interrupt_id && snapshot.interrupt_id !== previousInterruptId) {
    target.view.submittedInterruptId = ''
    target.view.rejected = ''
  }
  if (isTerminal(snapshot.status)) finalizeRecord(target)
  if (wasActive) refreshGitAfterRun(snapshot)
}

export async function syncRun(runRecord: RunRecord): Promise<void> {
  const snapshot = await api.getRun(apiConfig(), runRecord.id)
  applyRunSnapshot(runRecord, snapshot)
}

export async function submitApprovals(
  runId: string,
  interruptId: string,
  decisions: ApprovalDecision[],
): Promise<boolean> {
  const runRecord = findRunRecord(runId)
  if (!runRecord) return false
  clearBanner()
  try {
    const run = await api.submitApprovals(apiConfig(), runId, interruptId, decisions)
    runRecord.view.submittedInterruptId = interruptId
    runRecord.view.rejected = ''
    applyRunSnapshot(runRecord, run)
    streamRun(runRecord)
    return true
  } catch (error) {
    showError(error, '提交批准')
    if (error instanceof ApiError && error.status === 409) {
      await syncRun(runRecord).catch(() => undefined)
    }
    return false
  }
}

export async function submitHumanInput(
  runId: string,
  interruptId: string,
  values: Record<string, unknown>,
): Promise<boolean> {
  const runRecord = findRunRecord(runId)
  if (!runRecord) return false
  clearBanner()
  try {
    const run = await api.submitHumanInput(apiConfig(), runId, interruptId, values)
    runRecord.view.submittedInterruptId = interruptId
    runRecord.view.rejected = ''
    applyRunSnapshot(runRecord, run)
    streamRun(runRecord)
    return true
  } catch (error) {
    showError(error, '提交回答')
    if (error instanceof ApiError && error.status === 409) {
      await syncRun(runRecord).catch(() => undefined)
    }
    return false
  }
}

export async function continueRun(runId: string, interruptId: string): Promise<boolean> {
  const runRecord = findRunRecord(runId)
  if (!runRecord) return false
  clearBanner()
  try {
    const run = await api.continueRun(apiConfig(), runId, interruptId)
    runRecord.view.submittedInterruptId = interruptId
    applyRunSnapshot(runRecord, run)
    streamRun(runRecord)
    return true
  } catch (error) {
    showError(error, '继续运行')
    if (error instanceof ApiError && error.status === 409) {
      await syncRun(runRecord).catch(() => undefined)
    }
    return false
  }
}

export async function cancelRun(runId: string): Promise<void> {
  const runRecord = findRunRecord(runId)
  if (!runRecord || app.busy.cancel) return
  app.busy.cancel = true
  clearBanner()
  try {
    const run = await api.cancelRun(apiConfig(), runId)
    applyRunSnapshot(runRecord, run)
    streamRun(runRecord)
  } catch (error) {
    showError(error, '取消运行')
    if (error instanceof ApiError && error.status === 409) {
      await syncRun(runRecord).catch(() => undefined)
    }
  } finally {
    app.busy.cancel = false
  }
}

export async function refreshCurrent(): Promise<void> {
  const record = currentRecord.value
  if (!record) {
    await connect()
    return
  }
  clearBanner()
  try {
    const session = await api.getSession(apiConfig(), record.session.id)
    record.session = session
    await loadSessionHistory(session.id)
  } catch (error) {
    showError(error, '刷新会话')
  }
}

// ---------------------------------------------------------------- event stream

export function streamRun(record: RunRecord): void {
  const runRecord = canonicalRun(record)
  if (streamHandles.has(runRecord.id)) return
  if (isTerminal(runRecord.run.status) && runRecord.replayComplete) return
  const controller = new AbortController()
  streamHandles.set(runRecord.id, controller)
  void runStreamLoop(runRecord, controller.signal).finally(() => {
    if (streamHandles.get(runRecord.id) === controller) streamHandles.delete(runRecord.id)
  })
}

async function runStreamLoop(record: RunRecord, signal: AbortSignal): Promise<void> {
  const runRecord = canonicalRun(record)
  const refreshOnCompletion = !isTerminal(runRecord.run.status)
  let attempt = 0
  while (!signal.aborted) {
    try {
      for await (const message of streamEvents(apiConfig(), runRecord.id, runRecord.cursor || '0', signal)) {
        if (signal.aborted) break
        if (message.id && compareCursor(message.id, runRecord.cursor) <= 0) continue
        applySseMessage(runRecord, message)
        runRecord.cursor = message.id
        attempt = 0
        if (message.event === 'run_finished') {
          if (refreshOnCompletion) refreshGitAfterRun(runRecord.run)
          break
        }
      }
      if (signal.aborted) break
      // EOF is normal for a terminal run; otherwise re-check state and resume.
      await syncRun(runRecord).catch(() => undefined)
      if (isTerminal(runRecord.run.status)) {
        runRecord.replayComplete = true
        break
      }
    } catch {
      if (signal.aborted) break
      // A failed connection must not mark terminal replay complete: missing logs
      // still need fetching even if a concurrent GET already saw completion.
      await syncRun(runRecord).catch(() => undefined)
    }
    attempt += 1
    await sleep(Math.min(700 * attempt, 5000), signal)
  }

}

function compareCursor(a: string, b: string): number {
  try {
    const left = BigInt(a)
    const right = BigInt(b || '0')
    return left < right ? -1 : left > right ? 1 : 0
  } catch {
    return 0
  }
}

function resumeStreams(): void {
  const records = Object.values(app.records)
  let started = 0
  for (const record of records) {
    const latest = record.runs[record.runs.length - 1]
    if (latest && !isTerminal(latest.run.status) && started < 4) {
      streamRun(latest)
      started += 1
    }
  }
}

let pollTimer: ReturnType<typeof setInterval> | null = null

/** Safety net: GET /v1/runs/{id} while a run is not terminal. */
export function startPolling(): void {
  if (pollTimer) return
  pollTimer = setInterval(() => {
    void (async () => {
      for (const record of Object.values(app.records)) {
        const latest = record.runs[record.runs.length - 1]
        if (!latest || isTerminal(latest.run.status)) continue
        try {
          await syncRun(latest)
        } catch {
          /* polling is best effort; the stream loop reports the real error */
        }
      }
    })()
  }, 5000)
}

export function stopStreams(): void {
  for (const controller of streamHandles.values()) controller.abort()
  streamHandles.clear()
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/** Wipe the browser-side transcripts (the service keeps its own state). */
export function resetRecords(): void {
  stopStreams()
  clearDrafts()
  writeThreadLocation('')
  app.records = {}
  app.history = {}
  app.transcripts = {}
  startPolling()
  app.sessionId = ''
  app.view = 'home'
}

export function initApp(): void {
  startPolling()
  void connect()
}

export type { SseMessage }

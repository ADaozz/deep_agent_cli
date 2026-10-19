import type { Capabilities, PermissionSelection, GitCommits, WorkspaceDirectory, FilePreview } from './types'
import type { ApprovalDecision, Project, Run, Session, SseMessage, PaginatedRuns, ActiveRunResponse, ModelCatalog, ModelSelection, WorkspaceGit, Workspace, DirectoryListing, GitFileDiff, GitDiffScope } from './types'

export interface ApiConfig {
  baseUrl: string
}

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail))
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** A request that failed before any HTTP status was observed (DNS, CORS, offline). */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'NetworkError'
  }
}

export function normalizeBaseUrl(value: string): string {
  const trimmed = value.trim()
  if (!trimmed) return ''
  return trimmed.replace(/\/+$/, '')
}

export function endpoint(config: ApiConfig, path: string): string {
  const base = normalizeBaseUrl(config.baseUrl)
  if (base !== '/api/agent') throw new Error('请通过本地同源网关连接服务')
  return `${base}${path.replace(/^\/v1(?=\/)/, '')}`
}

function authHeaders(withBody: boolean): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (withBody) headers['Content-Type'] = 'application/json'
  return headers
}

async function readError(response: Response): Promise<never> {
  let detail: unknown = response.statusText || `HTTP ${response.status}`
  try {
    const text = await response.text()
    if (text) {
      try {
        const parsed = JSON.parse(text) as { detail?: unknown }
        detail = parsed?.detail ?? parsed
      } catch {
        detail = text
      }
    }
  } catch {
    /* keep status text */
  }
  throw new ApiError(response.status, detail)
}

async function request<T>(
  config: ApiConfig,
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const { method = 'GET', body, signal } = options
  let response: Response
  try {
    response = await fetch(endpoint(config, path), {
      method,
      headers: authHeaders(body !== undefined),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new NetworkError(error instanceof Error ? error.message : String(error))
  }
  if (!response.ok) await readError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export async function getHealth(baseUrl: string): Promise<boolean> {
  try {
    const response = await fetch(endpoint({ baseUrl }, '/health'), {
      headers: { Accept: 'application/json' },
    })
    if (!response.ok) return false
    const data = (await response.json()) as { status?: string }
    return data.status === 'ok'
  } catch {
    throw new NetworkError('无法访问服务地址')
  }
}

export const api = {
  workspaceFiles: (config: ApiConfig, projectId: string, path = '', offset = 0) =>
    request<WorkspaceDirectory>(config, `/v1/projects/${encodeURIComponent(projectId)}/files?path=${encodeURIComponent(path)}&offset=${offset}`),
  previewFile: (config: ApiConfig, projectId: string, path: string) =>
    request<FilePreview>(config, `/v1/projects/${encodeURIComponent(projectId)}/file-preview?path=${encodeURIComponent(path)}`),
  capabilities: (config: ApiConfig, projectId: string) => request<Capabilities>(config, `/v1/projects/${encodeURIComponent(projectId)}/capabilities`),
  permission: (config: ApiConfig, sessionId: string) => request<PermissionSelection>(config, `/v1/sessions/${encodeURIComponent(sessionId)}/permission`),
  selectPermission: (config: ApiConfig, sessionId: string, mode: 'ask' | 'allow', confirmation = '') => request<PermissionSelection>(config, `/v1/sessions/${encodeURIComponent(sessionId)}/permission`, { method: 'POST', body: { mode, confirmation } }),
  workspaces: (config: ApiConfig) => request<{ workspaces: Workspace[]; home_path?: string }>(config, '/v1/workspaces'),
  selectWorkspace: (config: ApiConfig, path: string) => request<Workspace>(config, '/v1/workspaces', { method: 'POST', body: { path } }),
  directories: (config: ApiConfig, path?: string) => request<DirectoryListing>(config, '/v1/workspace-directories', { method: 'POST', body: path ? { path } : {} }),
  projectGit: (config: ApiConfig, projectId: string) =>
    request<WorkspaceGit>(config, `/v1/projects/${encodeURIComponent(projectId)}/git`),
  projectCommits: (config: ApiConfig, projectId: string, cursor?: string) =>
    request<GitCommits>(config, `/v1/projects/${encodeURIComponent(projectId)}/git/commits?limit=20${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`),
  projectGitDiff: (config: ApiConfig, projectId: string, path: string, scope: GitDiffScope, full = false) =>
    request<GitFileDiff>(config, `/v1/projects/${encodeURIComponent(projectId)}/git/diff?path=${encodeURIComponent(path)}&scope=${scope}&full=${full ? '1' : '0'}`),
  models: (config: ApiConfig, projectId: string) =>
    request<ModelCatalog>(config, `/v1/projects/${encodeURIComponent(projectId)}/models`),
  sessionModel: (config: ApiConfig, sessionId: string) =>
    request<ModelSelection>(config, `/v1/sessions/${encodeURIComponent(sessionId)}/model`),
  selectModel: (config: ApiConfig, sessionId: string, modelId: string, effort: string) =>
    request<ModelSelection>(config, `/v1/sessions/${encodeURIComponent(sessionId)}/model`, { method: 'POST', body: { model_id: modelId, reasoning_effort: effort } }),
  sessionRuns: (config: ApiConfig, sessionId: string, cursor?: string) =>
    request<PaginatedRuns>(config, `/v1/sessions/${encodeURIComponent(sessionId)}/runs?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
  activeRun: (config: ApiConfig, projectId: string) =>
    request<ActiveRunResponse>(config, `/v1/projects/${encodeURIComponent(projectId)}/active-run`),
  projects: (config: ApiConfig) => request<{ projects: Project[] }>(config, '/v1/projects'),

  createSession: (config: ApiConfig, projectId: string) =>
    request<Session>(config, '/v1/sessions', { method: 'POST', body: { project_id: projectId } }),

  listSessions: (config: ApiConfig, projectId?: string) =>
    request<{ sessions: Session[] }>(
      config,
      projectId ? `/v1/sessions?project_id=${encodeURIComponent(projectId)}` : '/v1/sessions',
    ),

  getSession: (config: ApiConfig, sessionId: string) =>
    request<Session>(config, `/v1/sessions/${encodeURIComponent(sessionId)}`),

  deleteSession: (config: ApiConfig, sessionId: string) =>
    request<{ deleted: boolean }>(config, `/v1/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
      body: {},
    }),

  getRun: (config: ApiConfig, runId: string) =>
    request<Run>(config, `/v1/runs/${encodeURIComponent(runId)}`),

  createRun: (config: ApiConfig, sessionId: string, body: { text: string; file_paths?: string[] } | { resume: true }) =>
    request<Run>(config, `/v1/sessions/${encodeURIComponent(sessionId)}/runs`, {
      method: 'POST',
      body,
    }),

  submitApprovals: (
    config: ApiConfig,
    runId: string,
    interruptId: string,
    decisions: ApprovalDecision[],
  ) =>
    request<Run>(config, `/v1/runs/${encodeURIComponent(runId)}/approvals`, {
      method: 'POST',
      body: { interrupt_id: interruptId, decisions },
    }),

  submitHumanInput: (
    config: ApiConfig,
    runId: string,
    interruptId: string,
    values: Record<string, unknown>,
  ) =>
    request<Run>(config, `/v1/runs/${encodeURIComponent(runId)}/human-input`, {
      method: 'POST',
      body: { interrupt_id: interruptId, values },
    }),

  continueRun: (config: ApiConfig, runId: string, interruptId: string) =>
    request<Run>(config, `/v1/runs/${encodeURIComponent(runId)}/continue`, {
      method: 'POST',
      body: { interrupt_id: interruptId },
    }),

  cancelRun: (config: ApiConfig, runId: string) =>
    request<Run>(config, `/v1/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST', body: {} }),
}

interface ParsedFrame {
  id: string
  event: string
  data: string
}

function parseFrame(frame: string): ParsedFrame | null {
  let id = ''
  let event = 'message'
  const data: string[] = []
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const key = colon < 0 ? line : line.slice(0, colon)
    const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '')
    if (key === 'id') id = value
    else if (key === 'event') event = value
    else if (key === 'data') data.push(value)
  }
  if (data.length === 0) return null
  if (!/^\d+$/.test(id)) throw new Error('事件流中的 ID 无效')
  return { id, event, data: data.join('\n') }
}

/**
 * Fetch-based SSE reader through the local same-origin gateway. The service
 * credential is injected by the gateway and never reaches this client.
 * Frames are buffered until a full blank-line separator arrives; keepalive
 * comments are ignored. A trailing incomplete frame is dropped on purpose so
 * the cursor never advances past an event that was not fully applied.
 */
export async function* streamEvents(
  config: ApiConfig,
  runId: string,
  after: string,
  signal: AbortSignal,
): AsyncGenerator<SseMessage> {
  let response: Response
  try {
    response = await fetch(
      endpoint(config, `/v1/runs/${encodeURIComponent(runId)}/events?after=${encodeURIComponent(after)}`),
      { headers: { ...authHeaders(false), Accept: 'text/event-stream' }, signal },
    )
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new NetworkError(error instanceof Error ? error.message : String(error))
  }
  if (!response.ok) await readError(response)
  if (!response.body) throw new NetworkError('服务未返回事件流')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      let separator: RegExpExecArray | null
      while ((separator = /\r?\n\r?\n/.exec(buffer)) !== null) {
        const frame = buffer.slice(0, separator.index)
        buffer = buffer.slice(separator.index + separator[0].length)
        const parsed = parseFrame(frame)
        if (!parsed) continue
        yield { id: parsed.id, event: parsed.event, data: JSON.parse(parsed.data) }
      }
      if (done) return
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

/** Human readable detail for a 422 `detail` array or a string. */
export function describeDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === 'object') {
          const entry = item as { loc?: unknown[]; msg?: string }
          const loc = Array.isArray(entry.loc) ? entry.loc.join('.') : ''
          return [loc, entry.msg].filter(Boolean).join(': ')
        }
        return String(item)
      })
      .filter(Boolean)
      .join('; ')
  }
  if (detail && typeof detail === 'object') {
    try {
      return JSON.stringify(detail)
    } catch {
      return String(detail)
    }
  }
  return ''
}

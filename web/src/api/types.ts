/**
 * Types transcribed from docs/server.md §3 "数据结构" and §5 "SSE 事件协议".
 * They describe the documented service contract; nothing here invents a field
 * the service does not return.
 */

export type RunStatus =
  | 'running'
  | 'waiting_confirmation'
  | 'waiting_human'
  | 'paused'
  | 'runtime_config_boundary'
  | 'cancelling'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export const TERMINAL_STATUSES: RunStatus[] = ['completed', 'failed', 'cancelled', 'interrupted']

export const WAITING_STATUSES: RunStatus[] = [
  'waiting_confirmation',
  'waiting_human',
  'paused',
  'runtime_config_boundary',
]

export function isTerminal(status: RunStatus | undefined | null): boolean {
  return !!status && TERMINAL_STATUSES.includes(status)
}

export interface Project {
  id: string
}

export interface Session {
  id: string
  project_id: string
  created_at: string
  /** First submitted prompt, collapsed and capped. Empty when the service has none. */
  title?: string
}

export interface PendingTool {
  toolCallId: string
  name: string
  args: Record<string, unknown>
}

export interface InputOption {
  value: string
  label: string
  description: string
  /** Custom ("Other:") option marker. Never hardcode `__other__`. */
  allowsCustomText?: boolean
}

export type InputFieldType = 'text' | 'textarea' | 'single_select' | 'multi_select' | 'boolean'

export interface InputField {
  id: string
  type: InputFieldType
  label: string
  required: boolean
  placeholder: string
  options: InputOption[]
}

export type InteractionType = 'clarification' | 'decision' | 'confirmation' | 'review'

export interface HumanInput {
  type: 'human_input'
  interactionType: InteractionType | string
  title: string
  reason: string
  question: string
  fields: InputField[]
  blocking: boolean
  impact: string[]
  /** Agent-declared id; NOT the HTTP interrupt id. */
  interactionId?: string
  recommendation?: { value: string; reason: string } | null
}

export interface RunResult {
  status: RunStatus
  output: string
  pending_tool_calls: PendingTool[]
  human_input: HumanInput | Record<string, never>
  error: string
}

export interface Run {
  id: string
  session_id: string
  project_id: string
  status: RunStatus
  created_at: string
  updated_at: string
  result: Partial<RunResult>
  interrupt_id: string | null
  /** Submitted text persisted with the run. Absent on older rows. */
  prompt?: string
}

/** `agent_event.data` payload (§5 "Agent 事件"). */
export interface AgentEvent {
  type: string
  content: string
  message_id: string
  tool_call_id: string
  name: string
  arguments: Record<string, unknown>
  result: unknown
  is_error: boolean
  stream: string
  artifact: unknown
}

export interface ApprovalDecision {
  tool_call_id: string
  decision: 'approve' | 'reject'
  message?: string | null
}

export interface SseMessage {
  id: string
  event: string
  data: unknown
}

export function isHumanInput(value: unknown): value is HumanInput {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<HumanInput>
  return candidate.type === 'human_input' && Array.isArray(candidate.fields)
}

export interface RunSummary {
    id: string;
    session_id: string;
    project_id: string;
    status: RunStatus;
    created_at: string;
    updated_at: string;
}
export interface PaginatedRuns {
    runs: RunSummary[];
    next_cursor: string | null;
}
export interface ActiveRunResponse {
    run: RunSummary | null;
    blocked?: boolean;
}
export interface ModelSelection {
  model_id: string
  model: string
  source: string
  reasoning_effort: string
}
export interface ModelProfile {
  input?: ('text' | 'image')[]
  id: string
  model: string
  source: string
  reasoning_efforts: string[]
}
export interface ModelCatalog {
  models: ModelProfile[]
  current: ModelSelection
}

export interface GitFileEntry {
  path: string
  added: number | null
  removed: number | null
  status: string
  binary: boolean
  staged?: boolean
  unstaged?: boolean
  untracked?: boolean
  old_path?: string | null
}
export interface WorkspaceGit {
  available: boolean
  branch?: string
  branches?: string[]
  remote_branches?: string[]
  changed?: number
  head?: string | null
  base_branch?: string | null
  merged?: boolean | null
  added?: number | null
  removed?: number | null
  files?: GitFileEntry[]
}
export type GitDiffScope = 'all' | 'staged' | 'unstaged'
export interface GitDiffLine {
  kind: 'context' | 'add' | 'delete' | 'meta'
  text: string
  old_no: number | null
  new_no: number | null
}
export interface GitDiffHunk {
  header: string
  old_start: number
  old_count: number
  new_start: number
  new_count: number
  lines: GitDiffLine[]
}
export interface GitFileDiff {
  available: boolean
  path?: string
  scope?: GitDiffScope
  status?: string
  old_path?: string | null
  binary?: boolean
  symlink?: boolean
  target?: string | null
  untracked?: boolean
  truncated?: boolean
  hunks?: GitDiffHunk[]
  error?: string
}

export interface Workspace extends Project { path: string }
export interface DirectoryListing {
  path: string
  parent: string | null
  directories: { name: string; path: string }[]
  truncated: boolean
}

export interface SkillReference { name: string; path: string; instruction: string; description: string }
export interface Capabilities { skills: SkillReference[]; warnings: string[]; allow_available: boolean; allow_warning: string }
export interface PermissionSelection { mode: 'ask' | 'allow' }

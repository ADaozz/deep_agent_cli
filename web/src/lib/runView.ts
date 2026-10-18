import type { AgentEvent, HumanInput, Run, SseMessage } from '../api/types'
import { isHumanInput } from '../api/types'

export interface AssistantBlock {
  id: string
  /** Cumulative assistant text from the `delta` channel (replace, never append). */
  text: string
  /** Authoritative text from `assistant_completed` / final `result.output`. */
  final: string
  thinking: string
  done: boolean
  /** Wall-clock ms when reasoning first arrived (drives "Thought for 3s"). */
  thinkingStartedAt?: number
  /** Wall-clock ms when text/tools resumed; 0 while reasoning is still open. */
  thinkingEndedAt?: number
}

export type ToolState = 'started' | 'ok' | 'error'

export interface ToolView {
  id: string
  name: string
  args: Record<string, unknown>
  /** Bounded preview of the streamed output (tail_snapshot replaces). */
  output: string
  stream: string
  truncated: boolean
  state: ToolState
  isError: boolean
  artifact: unknown
  /** Full content from tool_completed, shown when no stream output was seen. */
  content: string
}

export interface Notice {
  id: string
  text: string
  kind: 'info' | 'warn'
}

export interface UsageReadout {
  input: number | null
  output: number | null
  total: number | null
}

export type TimelineItem = { kind: 'block' | 'tool' | 'notice'; id: string }

export interface RunView {
  blocks: AssistantBlock[]
  currentBlockId: string
  toolOrder: string[]
  tools: Record<string, ToolView>
  /** Display order of blocks/tools/notices for this run. */
  items: TimelineItem[]
  usage: UsageReadout
  notices: Notice[]
  /** interrupt_id of a form already submitted in this page (disables it). */
  submittedInterruptId: string
  /** Last interaction error reported by the service. */
  rejected: string
  /** True once run_finished / a terminal GET snapshot was applied. */
  finalized: boolean
}

/** A run plus the locally reconstructed display state (server summaries and event replay restore missing local records). */
export interface RunRecord {
  id: string
  sessionId: string
  prompt: string
  resume: boolean
  createdAt: string
  /** Last applied SSE event id; the replay cursor for this run only. */
  cursor: string
  replayComplete?: boolean
  run: Run
  view: RunView
}

export interface SessionRecord {
  session: { id: string; project_id: string; created_at: string }
  title: string
  runs: RunRecord[]
  draft: string
}

/** Tool output kept in the browser is capped: the full log is not downloadable over HTTP. */
export const MAX_TOOL_OUTPUT = 32_000
const MAX_BLOCKS_TEXT = 200_000

export function emptyView(): RunView {
  return {
    blocks: [],
    currentBlockId: '',
    toolOrder: [],
    tools: {},
    items: [],
    usage: { input: null, output: null, total: null },
    notices: [],
    submittedInterruptId: '',
    rejected: '',
    finalized: false,
  }
}

function cap(value: string, limit = MAX_TOOL_OUTPUT): string {
  if (value.length <= limit) return value
  return `${value.slice(0, limit)}\n… (output truncated: the console keeps a bounded preview)`
}

function ensureBlock(view: RunView): AssistantBlock {
  const existing = view.blocks.find((block) => block.id === view.currentBlockId)
  if (existing) return existing
  return startBlock(view)
}

/** Record the reasoning window so the log can label it "Thought for 3s". */
function openThinking(block: AssistantBlock, text: string): void {
  if (!text.trim()) return
  if (!block.thinkingStartedAt) block.thinkingStartedAt = Date.now()
  // Later cumulative reasoning snapshots must not reopen a window that already
  // closed, or the label grows to the whole turn.
}

/** Reasoning gave way to prose/tools: close the window once. */
function closeThinking(block: AssistantBlock): void {
  if (block.thinkingStartedAt && !block.thinkingEndedAt) block.thinkingEndedAt = Date.now()
}

function startBlock(view: RunView): AssistantBlock {
  const block: AssistantBlock = {
    id: `b${view.blocks.length + 1}`,
    text: '',
    final: '',
    thinking: '',
    done: false,
  }
  view.blocks.push(block)
  view.currentBlockId = block.id
  pushItem(view, 'block', block.id)
  return block
}

function pushItem(view: RunView, kind: TimelineItem['kind'], id: string): void {
  if (!Array.isArray(view.items)) view.items = []
  view.items.push({ kind, id })
}

function addNotice(view: RunView, text: string, kind: Notice['kind'] = 'info'): void {
  const notice: Notice = { id: `n${view.notices.length + 1}`, text, kind }
  view.notices.push(notice)
  pushItem(view, 'notice', notice.id)
  if (view.notices.length > 50) view.notices.splice(0, view.notices.length - 50)
}

/** Render order, with a fallback for records persisted before `items` existed. */
export function displayItems(view: RunView): TimelineItem[] {
  if (Array.isArray(view.items) && view.items.length) return view.items
  return [
    ...view.blocks.map((block) => ({ kind: 'block' as const, id: block.id })),
    ...view.toolOrder.map((id) => ({ kind: 'tool' as const, id })),
    ...view.notices.map((notice) => ({ kind: 'notice' as const, id: notice.id })),
  ]
}

function readInt(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
}

function upsertTool(view: RunView, event: AgentEvent): ToolView {
  const id = event.tool_call_id || `tool-${view.toolOrder.length + 1}`
  let tool = view.tools[id]
  if (!tool) {
    tool = {
      id,
      name: event.name || 'tool',
      args: {},
      output: '',
      stream: '',
      truncated: false,
      state: 'started',
      isError: false,
      artifact: null,
      content: '',
    }
    view.tools[id] = tool
    view.toolOrder.push(id)
    pushItem(view, 'tool', id)
  }
  if (event.name) tool.name = event.name
  // `tool_completed` repeats the call id without `arguments`; an empty object
  // must not erase the args captured by `tool_started`.
  if (
    event.arguments &&
    typeof event.arguments === 'object' &&
    Object.keys(event.arguments as Record<string, unknown>).length
  ) {
    tool.args = event.arguments as Record<string, unknown>
  }
  return tool
}

export function applyAgentEvent(view: RunView, event: AgentEvent): void {
  switch (event.type) {
    case 'assistant_started': {
      startBlock(view)
      return
    }
    case 'assistant_delta': {
      // The service-level `delta` channel already fills the block; only fall
      // back to this payload when no delta frame arrived for the call.
      const block = ensureBlock(view)
      if (typeof event.content === 'string') block.text = cap(event.content, MAX_BLOCKS_TEXT)
      if (block.text.trim()) closeThinking(block)
      return
    }
    case 'thinking_delta': {
      const block = ensureBlock(view)
      if (typeof event.content === 'string') block.thinking = cap(event.content, MAX_BLOCKS_TEXT)
      openThinking(block, block.thinking)
      return
    }
    case 'assistant_completed': {
      const block = ensureBlock(view)
      if (typeof event.content === 'string' && event.content.trim()) {
        block.final = cap(event.content, MAX_BLOCKS_TEXT)
      }
      const thinking = (event.result as { thinking?: unknown } | null)?.thinking
      if (typeof thinking === 'string' && thinking.trim()) {
        block.thinking = thinking
        openThinking(block, thinking)
      }
      closeThinking(block)
      block.done = true
      return
    }
    case 'tool_started': {
      const active = view.blocks.find((block) => block.id === view.currentBlockId)
      if (active) closeThinking(active)
      upsertTool(view, event)
      return
    }
    case 'tool_output_delta': {
      const tool = upsertTool(view, event)
      const content = typeof event.content === 'string' ? event.content : ''
      if (event.stream === 'tail_snapshot') {
        tool.output = cap(content)
        tool.truncated = true
      } else {
        tool.output = cap(tool.output + content)
      }
      tool.stream = event.stream || tool.stream
      return
    }
    case 'tool_completed': {
      const tool = upsertTool(view, event)
      const artifact = event.artifact as { exit_code?: number } | null
      tool.isError = !!event.is_error || (typeof artifact?.exit_code === 'number' && artifact.exit_code !== 0)
      tool.state = tool.isError ? 'error' : 'ok'
      if (typeof event.content === 'string' && event.content) tool.content = cap(event.content)
      if (event.artifact !== undefined && event.artifact !== null) tool.artifact = event.artifact
      return
    }
    case 'usage': {
      const result = event.result as Record<string, unknown> | null | undefined
      if (!result || typeof result !== 'object') return
      const input = readInt(result.input_tokens)
      const output = readInt(result.output_tokens)
      const total = readInt(result.total_tokens)
      // An empty usage event keeps the previous reading (CLI behaviour).
      if (input !== null) view.usage.input = input
      if (output !== null) view.usage.output = output
      if (total !== null) view.usage.total = total
      return
    }
    case 'compaction_started': {
      addNotice(view, '正在压缩上下文…')
      return
    }
    case 'compaction_finished': {
      addNotice(view, '上下文已压缩')
      return
    }
    case 'turn_completed':
      // Elapsed time already shows in the "Worked for …" row; no extra notice.
      return
    default:
      // Unknown display events are ignored; run status always comes from snapshots.
      return
  }
}

export function finalizeRecord(record: RunRecord): void {
  const view = record.view
  if (view.finalized) return
  view.finalized = true
  view.blocks.forEach((block) => {
    block.done = true
  })
  const output = typeof record.run.result?.output === 'string' ? record.run.result.output : ''
  if (!output.trim()) return
  const block = ensureBlock(view)
  block.final = cap(output, MAX_BLOCKS_TEXT)
  block.done = true
}

export function applySseMessage(record: RunRecord, message: SseMessage): void {
  const view = record.view
  switch (message.event) {
    case 'run_started':
    case 'interaction_requested':
    case 'run_finished': {
      const snapshot = message.data as Run
      if (snapshot && typeof snapshot === 'object' && snapshot.id) {
        if (!record.run.updated_at || snapshot.updated_at >= record.run.updated_at) record.run = snapshot
        if (message.event === 'interaction_requested') {
          view.submittedInterruptId = ''
          view.rejected = ''
        }
        if (message.event === 'run_finished') { finalizeRecord(record); record.replayComplete = true }
      }
      return
    }
    case 'delta': {
      const payload = message.data as { kind?: string; text?: string }
      if (typeof payload?.text !== 'string') return
      const block = ensureBlock(view)
      if (payload.kind === 'reasoning') {
        block.thinking = cap(payload.text, MAX_BLOCKS_TEXT)
        openThinking(block, block.thinking)
      } else {
        block.text = cap(payload.text, MAX_BLOCKS_TEXT)
        if (block.text.trim()) closeThinking(block)
      }
      return
    }
    case 'agent_event': {
      applyAgentEvent(view, message.data as AgentEvent)
      return
    }
    case 'interaction_submitted': {
      const payload = message.data as { interrupt_id?: string }
      view.submittedInterruptId = payload?.interrupt_id || record.run.interrupt_id || ''
      view.rejected = ''
      return
    }
    case 'interaction_rejected': {
      const payload = message.data as { error?: string }
      view.rejected = payload?.error || '交互响应无效'
      view.submittedInterruptId = ''
      return
    }
    default:
      return
  }
}

export type InteractionKind = 'approvals' | 'human' | 'continue' | 'unavailable'

export interface InteractionModel {
  kind: InteractionKind
  runId: string
  interruptId: string | null
  status: Run['status']
  pendingTools: { toolCallId: string; name: string; args: Record<string, unknown> }[]
  humanInput: HumanInput | null
}

/**
 * The interaction card is derived from the authoritative Run snapshot, never
 * from a cached form. `interrupt_id` must be captured before the POST, because
 * the service clears `result` and `interrupt_id` once an interaction is
 * accepted.
 */
export function interactionOf(record: RunRecord | null | undefined): InteractionModel | null {
  if (!record) return null
  const run = record.run
  if (!run) return null
  const pendingTools = Array.isArray(run.result?.pending_tool_calls)
    ? run.result.pending_tool_calls
    : []
  const humanInput = isHumanInput(run.result?.human_input) ? run.result.human_input : null
  switch (run.status) {
    case 'waiting_confirmation':
      return {
        kind: 'approvals',
        runId: run.id,
        interruptId: run.interrupt_id,
        status: run.status,
        pendingTools,
        humanInput: null,
      }
    case 'waiting_human':
      return {
        kind: 'human',
        runId: run.id,
        interruptId: run.interrupt_id,
        status: run.status,
        pendingTools: [],
        humanInput,
      }
    case 'paused':
    case 'runtime_config_boundary':
      return {
        kind: 'continue',
        runId: run.id,
        interruptId: run.interrupt_id,
        status: run.status,
        pendingTools: [],
        humanInput: null,
      }
    default:
      return null
  }
}

/** Labels used across the UI; keeps status wording in one place. */
export const STATUS_LABELS: Record<Run['status'], string> = {
  running: '运行中',
  waiting_confirmation: '等待批准',
  waiting_human: '等待输入',
  paused: '已暂停',
  runtime_config_boundary: '配置边界',
  cancelling: '停止中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
}

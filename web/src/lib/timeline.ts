/**
 * Display model for the Cursor-style thread: a flat work log plus a markdown
 * answer, derived only from documented run snapshots and agent events.
 *
 * Nothing here invents data: elapsed time comes from `created_at`/`updated_at`
 * on the Run and tool rows come from the main Runner event stream. Workspace
 * Git changes are queried separately; no per-run file-change records are built.
 */
import type { RunStatus } from '../api/types'
import { displayItems, type RunRecord, type ToolView } from './runView'

export interface LogRow {
  key: string
  kind: 'thinking' | 'tool' | 'notice' | 'prose'
  verb?: string
  detail?: string
  /** Full text for prose/notice/thinking rows. */
  text?: string
  /** Trailing affordance rendered in grey ("›"). */
  tail?: string
  tool?: ToolView
  isError?: boolean
}

export interface RunTimeline {
  prompt: string
  /** "Worked for 2m 11s" */
  worked: string
  rows: LogRow[]
  /** Markdown of the assistant answer (empty while nothing was produced). */
  answer: string
  status: RunStatus
  running: boolean
  error: string
}

/**
 * "Thought for 3 seconds" when the reasoning window was observed on the
 * stream, "Thought briefly" otherwise (older local records have no stamps).
 */
function thinkingLabel(block: { thinkingStartedAt?: number; thinkingEndedAt?: number }): string {
  const start = block.thinkingStartedAt
  if (!start) return '思考了片刻'
  const end = block.thinkingEndedAt || Date.now()
  const seconds = Math.round((end - start) / 1000)
  if (!Number.isFinite(seconds) || seconds < 1) return '思考了片刻'
  if (seconds < 60) return `思考了 ${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest ? `思考了 ${minutes} 分 ${rest} 秒` : `思考了 ${minutes} 分`
}

function pickString(args: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = args?.[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function brief(args: Record<string, unknown>): string {
  for (const value of Object.values(args ?? {})) {
    if (typeof value === 'string' && value.trim()) {
      const text = value.trim().replace(/\s+/g, ' ')
      return text.length > 88 ? `${text.slice(0, 88)}…` : text
    }
  }
  return ''
}

export interface ToolSummary {
  verb: string
  detail: string
  /** True for tools that modify the workspace. */
  writes: boolean
  path: string
}

export function summarizeTool(tool: ToolView): ToolSummary {
  return summarizeCall(tool.name || 'tool', tool.args ?? {})
}

/** Same verb/detail mapping for a pending (not yet streaming) tool call. */
export function summarizeCall(name: string, args: Record<string, unknown>): ToolSummary {
  const lower = name.toLowerCase()
  const path = pickString(args ?? {}, [
    'file_path',
    'path',
    'filename',
    'file',
    'target_file',
    'notebook_path',
    'directory',
  ])
  const command = pickString(args ?? {}, ['command', 'cmd', 'script', 'shell'])
  const pattern = pickString(args ?? {}, ['pattern', 'query', 'glob', 'regex', 'regexp', 'search', 'q'])

  const summary = (verb: string, detail: string, writes = false): ToolSummary => ({
    verb,
    detail,
    writes,
    path: writes ? path : '',
  })

  if (/write|create_file|save_file/.test(lower)) return summary('写入', path || brief(args), true)
  if (/(edit|patch|str_replace|replace|insert)/.test(lower)) {
    return summary('编辑', path || brief(args), true)
  }
  if (/(delete|remove|unlink)/.test(lower)) return summary('删除', path || brief(args), true)
  if (/(read|cat|open_file|view_file)/.test(lower)) return summary('读取', path || brief(args))
  if (/(search|grep|find|glob)/.test(lower)) {
    return summary('搜索文件', pattern || brief(args))
  }
  if (/(list|ls|dir)/.test(lower)) return summary('列出', path || brief(args))
  if (/(exec|shell|bash|command|run|terminal)/.test(lower)) {
    return summary('运行', command || brief(args))
  }
  if (/(web|fetch|http|browse)/.test(lower)) return summary('使用', 'web-fetch')
  if (/(todo|plan)/.test(lower)) return summary('更新', 'todos')
  return summary(name, brief(args))
}

export function durationLabel(startIso: string, endIso: string): string {
  const start = Date.parse(startIso)
  const end = Date.parse(endIso)
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return '片刻'
  const seconds = Math.max(1, Math.round((end - start) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  return mins ? `${hours} 小时 ${mins} 分` : `${hours} 小时`
}

export function buildTimeline(record: RunRecord): RunTimeline {
  const view = record.view
  const items = displayItems(view)

  const blocksWithText = view.blocks.filter((block) => (block.final || block.text || '').trim())
  // While the run waits for the user, the assistant text is mid-conversation
  // prose (it belongs above the question), not the final answer.
  const awaitingUser = [
    'waiting_confirmation',
    'waiting_human',
    'paused',
    'runtime_config_boundary',
  ].includes(record.run.status)
  const answerBlock = !awaitingUser && blocksWithText.length ? blocksWithText[blocksWithText.length - 1] : null
  const snapshotOutput =
    typeof record.run.result?.output === 'string' ? record.run.result.output.trim() : ''
  const answer =
    awaitingUser ? '' : snapshotOutput || (answerBlock ? (answerBlock.final || answerBlock.text).trim() : '')

  const rows: LogRow[] = []

  for (const item of items) {
    if (item.kind === 'tool') {
      const tool = view.tools[item.id]
      if (!tool) continue
      const info = summarizeTool(tool)
      const tail = tool.output || tool.content ? '›' : ''
      rows.push({
        key: `t-${tool.id}`,
        kind: 'tool',
        verb: info.verb,
        detail: info.detail,
        tail,
        tool,
        isError: tool.state === 'error',
      })
      continue
    }
    if (item.kind === 'notice') {
      const notice = view.notices.find((entry) => entry.id === item.id)
      if (notice) rows.push({ key: `n-${notice.id}`, kind: 'notice', text: notice.text })
      continue
    }
    const block = view.blocks.find((entry) => entry.id === item.id)
    if (!block) continue
    if (block.thinking.trim()) {
      rows.push({
        key: `think-${block.id}`,
        kind: 'thinking',
        verb: thinkingLabel(block),
        detail: '',
        text: block.thinking.trim(),
        tail: '›',
      })
    }
    const prose = (block.text || '').trim()
    if (block.id !== answerBlock?.id && prose) {
      rows.push({ key: `p-${block.id}`, kind: 'prose', text: prose })
    }
  }

  const running = !['completed', 'failed', 'cancelled', 'interrupted'].includes(record.run.status)
  const error = typeof record.run.result?.error === 'string' ? record.run.result.error.trim() : ''

  return {
    prompt: record.prompt,
    worked: `工作了 ${durationLabel(record.run.created_at, record.run.updated_at)}`,
    rows,
    answer,
    status: record.run.status,
    running,
    error,
  }
}

export const STATUS_UI: Record<RunStatus, { label: string; tone: 'ok' | 'run' | 'wait' | 'bad' | 'idle' }> = {
  running: { label: '运行中', tone: 'run' },
  waiting_confirmation: { label: '等待批准', tone: 'wait' },
  waiting_human: { label: '等待输入', tone: 'wait' },
  paused: { label: '已暂停', tone: 'wait' },
  runtime_config_boundary: { label: '已暂停', tone: 'wait' },
  cancelling: { label: '停止中', tone: 'run' },
  completed: { label: '已完成', tone: 'ok' },
  failed: { label: '失败', tone: 'bad' },
  cancelled: { label: '已取消', tone: 'idle' },
  interrupted: { label: '已中断', tone: 'bad' },
}

export function shortAge(iso: string): string {
  const time = Date.parse(iso)
  if (Number.isNaN(time)) return ''
  const diff = Date.now() - time
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (diff < minute) return '刚刚'
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟`
  if (diff < day) return `${Math.floor(diff / hour)} 小时`
  if (diff < 7 * day) return `${Math.floor(diff / day)} 天`
  if (diff < 30 * day) return `${Math.floor(diff / (7 * day))} 周`
  return `${Math.floor(diff / (30 * day))} 个月`
}

export function longAge(iso: string): string {
  const time = Date.parse(iso)
  if (Number.isNaN(time)) return ''
  const diff = Date.now() - time
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (diff < minute) return '刚刚'
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`
  if (diff < 7 * day) return `${Math.floor(diff / day)} 天前`
  return new Date(time).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

/** Basename for compact log rows: keep repo-relative tail of a long path. */
export function shortPath(path: string, max = 52): string {
  if (path.length <= max) return path
  return `…${path.slice(path.length - max + 1)}`
}

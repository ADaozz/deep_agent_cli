import { clearDrafts } from './drafts'
import type { Run } from '../api/types'
import type { RunRecord, SessionRecord } from '../lib/runView'
import { emptyView } from '../lib/runView'

const STORAGE_KEY = 'deepagent.records.v1'
const LAST_SESSION_KEY = 'deepagent.last-session.v1'

export type RecordMap = Record<string, SessionRecord>

/** Keep the browser copy small; older runs can be reloaded from the server. */
const MAX_RUNS_PER_SESSION = 20
const MAX_SESSIONS = 40

function prune(records: RecordMap): RecordMap {
  const entries = Object.entries(records)
    .sort((a, b) => b[1].session.created_at.localeCompare(a[1].session.created_at))
    .slice(0, MAX_SESSIONS)
  const next: RecordMap = {}
  for (const [key, record] of entries) {
    const runs = record.runs.slice(-MAX_RUNS_PER_SESSION)
    next[key] = { ...record, runs }
  }
  return next
}

export function loadRecords(): RecordMap {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as RecordMap
    if (!parsed || typeof parsed !== 'object') return {}
    const result: RecordMap = {}
    for (const [key, value] of Object.entries(parsed)) {
      if (!value || typeof value !== 'object' || !value.session?.id) continue
      result[key] = {
        session: value.session,
        title: typeof value.title === 'string' ? value.title : '',
        draft: typeof value.draft === 'string' ? value.draft : '',
        runs: Array.isArray(value.runs)
          ? value.runs
              .filter((run) => run && run.id && run.run)
              .map((run) => ({
                ...run,
                view: { ...emptyView(), ...run.view },
              }))
          : [],
      }
    }
    return result
  } catch {
    return {}
  }
}

let saveTimer: ReturnType<typeof setTimeout> | null = null

export function saveRecords(records: RecordMap, immediate = false): void {
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = null
  }
  const write = () => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(prune(records)))
    } catch {
      /* quota exceeded: the in-memory transcript still works */
    }
  }
  if (immediate) write()
  else saveTimer = setTimeout(write, 250)
}

export function clearRecords(): void {
  clearDrafts()
  try {
    localStorage.removeItem(STORAGE_KEY)
    localStorage.removeItem(LAST_SESSION_KEY)
  } catch {
    /* ignore */
  }
}

export function loadLastSessionId(): string {
  try {
    return localStorage.getItem(LAST_SESSION_KEY) || ''
  } catch {
    return ''
  }
}

export function saveLastSessionId(sessionId: string): void {
  try {
    if (sessionId) localStorage.setItem(LAST_SESSION_KEY, sessionId)
    else localStorage.removeItem(LAST_SESSION_KEY)
  } catch {
    /* ignore */
  }
}

export function makeRunRecord(run: Run, prompt: string, resume: boolean): RunRecord {
  return {
    id: run.id,
    sessionId: run.session_id,
    prompt,
    resume,
    createdAt: run.created_at,
    cursor: '0',
    run,
    view: emptyView(),
  }
}

export function latestRun(record: SessionRecord | null | undefined): RunRecord | null {
  if (!record || record.runs.length === 0) return null
  return record.runs[record.runs.length - 1]
}

import type { Run } from '../api/types'
import type { RunRecord, SessionRecord } from '../lib/runView'
import { emptyView } from '../lib/runView'

export type RecordMap = Record<string, SessionRecord>

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

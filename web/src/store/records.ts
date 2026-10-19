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

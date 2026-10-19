import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { Run, RunStatus } from '../api/types'
import * as client from '../api/client'
import { app, applyRunSnapshot, stopStreams, streamRun } from '../store/app'
import { makeRunRecord } from '../store/records'
import { refreshGit } from '../store/git'
import { workspaceFileVersions } from '../store/files'

vi.mock('../store/git', () => ({ refreshGit: vi.fn(), workspaceGit: {} }))

function run(id: string, status: RunStatus): Run {
  return { id, session_id: 's', project_id: 'project', status,
    created_at: '2026-10-19T00:00:00Z', updated_at: '2026-10-19T00:01:00Z',
    result: {}, interrupt_id: null }
}

beforeEach(() => { app.records = {}; vi.clearAllMocks() })
afterEach(() => { stopStreams(); vi.restoreAllMocks() })

it.each<RunStatus>(['completed', 'failed', 'cancelled', 'interrupted'])('refreshes Git once when a snapshot ends a run as %s', status => {
  const record = makeRunRecord(run(status, 'running'), '', false)
  const previousVersion = workspaceFileVersions.project || 0
  applyRunSnapshot(record, run(status, 'waiting_human'))
  expect(refreshGit).not.toHaveBeenCalled()
  applyRunSnapshot(record, run(status, status))
  applyRunSnapshot(record, run(status, status))
  expect(refreshGit).toHaveBeenCalledExactlyOnceWith('project')
  expect(workspaceFileVersions.project).toBe(previousVersion + 1)
})

it('refreshes after SSE completion without repeating for the final GET snapshot', async () => {
  const finished = run('stream', 'completed')
  const record = makeRunRecord(run('stream', 'running'), '', false)
  vi.spyOn(client, 'streamEvents').mockImplementation(async function* () {
    yield { id: '1', event: 'run_finished', data: finished }
  })
  vi.spyOn(client.api, 'getRun').mockResolvedValue(finished)
  streamRun(record)
  await vi.waitFor(() => expect(client.api.getRun).toHaveBeenCalled())
  expect(refreshGit).toHaveBeenCalledExactlyOnceWith('project')
})

it('does not refresh Git when replaying an already completed historical run', async () => {
  const finished = run('history', 'completed')
  const record = makeRunRecord(finished, '', false)
  vi.spyOn(client, 'streamEvents').mockImplementation(async function* () {
    yield { id: '1', event: 'run_started', data: run('history', 'running') }
    yield { id: '2', event: 'run_finished', data: finished }
  })
  vi.spyOn(client.api, 'getRun').mockResolvedValue(finished)
  streamRun(record)
  await vi.waitFor(() => expect(client.api.getRun).toHaveBeenCalled())
  expect(refreshGit).not.toHaveBeenCalled()
})

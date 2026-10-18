import { afterEach, expect, it, vi } from 'vitest'
import type { Run } from '../api/types'

let stop: (() => void) | undefined
afterEach(() => { stop?.(); vi.unstubAllGlobals() })
it('paginates server history, preserves local prompts and fully replays terminal runs', async () => {
  vi.resetModules()
  localStorage.clear()
  const session = { id: 's', project_id: 'app', created_at: '2026-10-18T00:00:00Z' }
  const make = (id: string, minute: number): Run => ({ id, session_id: 's', project_id: 'app', status: 'completed', created_at: `2026-10-18T00:0${minute}:00Z`, updated_at: `2026-10-18T00:0${minute}:30Z`, result: { output: id + ' done' }, interrupt_id: null })
  const newer = make('new', 2), older = make('old', 1)
  const fetch = vi.fn(async (input: string) => {
    const url = new URL(input, 'http://localhost')
    if (url.pathname.endsWith('/sessions/s/runs')) return new Response(JSON.stringify(url.searchParams.has('cursor') ? { runs: [older], next_cursor: null } : { runs: [newer], next_cursor: 'PAGE_2' }))
    const snapshot = url.pathname.includes('/old') ? older : newer
    if (url.pathname.endsWith('/events')) {
      const events = [
        ['run_started', { ...snapshot, status: 'running', updated_at: snapshot.created_at }],
        ['agent_event', { type: 'tool_started', tool_call_id: 't', name: 'execute', arguments: { command: 'printf real-output' } }],
        ['agent_event', { type: 'tool_completed', tool_call_id: 't', name: 'execute', content: 'real-output', is_error: false }],
        ['run_finished', snapshot],
      ]
      return new Response(events.map(([event, data], i) => `id: ${i + 1}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(''))
    }
    return new Response(JSON.stringify(snapshot))
  })
  vi.stubGlobal('fetch', fetch)
  const store = await import('../store/app')
  stop = store.stopStreams
  const { makeRunRecord } = await import('../store/records')
  const record = store.ensureRecord(session)
  store.attachRun(record, makeRunRecord(newer, 'locally remembered prompt', false))
  await store.loadSessionHistory('s')
  await vi.waitFor(() => expect(record.runs[0].replayComplete).toBe(true))
  expect(record.runs[0].view.tools.t.content).toBe('real-output')
  expect(record.runs[0].prompt).toBe('locally remembered prompt')
  expect(record.runs[0].run.status).toBe('completed')
  expect(store.app.history.s.cursor).toBe('PAGE_2')
  await store.loadSessionHistory('s', true)
  await vi.waitFor(() => expect(record.runs[0].replayComplete).toBe(true))
  expect(record.runs.map(run => run.id)).toEqual(['old', 'new'])
  expect(record.runs[0].prompt).toContain('原始指令不可用')
  expect(record.runs[0].view.tools.t.content).toBe('real-output')
  expect(store.app.history.s.cursor).toBeNull()
  expect(fetch.mock.calls.some(([url]) => url.includes('cursor=PAGE_2'))).toBe(true)
})

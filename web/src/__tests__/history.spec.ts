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
  expect(record.title).toBe('locally remembered prompt')
  expect(record.runs[0].view.tools.t.content).toBe('real-output')
  expect(store.app.history.s.cursor).toBeNull()
  expect(fetch.mock.calls.some(([url]) => url.includes('cursor=PAGE_2'))).toBe(true)
})

it('restores a persisted prompt into the thread and the sidebar title', async () => {
  vi.resetModules()
  const session = { id: 's', project_id: 'app', created_at: '2026-10-18T00:00:00Z', title: '审查登录' }
  const run: Run = { id: 'r', session_id: 's', project_id: 'app', status: 'completed', created_at: '2026-10-18T00:01:00Z', updated_at: '2026-10-18T00:01:30Z', result: { output: 'done' }, interrupt_id: null, prompt: '审查登录流程' }
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = String(input)
    if (url.endsWith('/sessions')) return new Response(JSON.stringify({ sessions: [session] }))
    if (url.includes('/sessions/s/runs')) return new Response(JSON.stringify({ runs: [run], next_cursor: null }))
    if (url.includes('/events')) return new Response('')
    return new Response(JSON.stringify(run))
  }))
  const store = await import('../store/app')
  stop = store.stopStreams
  await store.loadSessions()
  expect(store.sessionRows.value.find((row) => row.id === 's')?.title).toBe('审查登录')
  await store.loadSessionHistory('s')
  expect(store.app.records.s.runs[0].prompt).toBe('审查登录流程')
})

it('removes the empty session when the first run is rejected', async () => {
  vi.resetModules()
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const method = init?.method || 'GET'
    calls.push(`${method} ${String(input)}`)
    if (method === 'POST' && String(input).endsWith('/sessions')) {
      return new Response(JSON.stringify({ id: 'empty', project_id: 'app', created_at: '2026-10-18T00:00:00Z' }), { status: 201 })
    }
    if (method === 'POST' && String(input).includes('/runs')) {
      return new Response(JSON.stringify({ detail: 'Project already has an unfinished run' }), { status: 409 })
    }
    if (method === 'DELETE') return new Response(JSON.stringify({ deleted: true }))
    return new Response('{}', { status: 404 })
  }))
  const store = await import('../store/app')
  stop = store.stopStreams
  store.app.projects = [{ id: 'app', path: '/repo' }]
  store.app.draftProjectId = 'app'
  expect(await store.sendFromHome('hello')).toBe(false)
  expect(store.app.sessions.some((item) => item.id === 'empty')).toBe(false)
  expect(store.app.sessionId).toBe('')
  expect(calls.some((call) => call.startsWith('DELETE '))).toBe(true)
})

it('appends to the URL thread instead of creating a session during connect', async () => {
  vi.resetModules()
  window.location.hash = 'thread=existing'
  const existing = { id: 'existing', project_id: 'app', created_at: '2026-10-18T00:00:00Z', title: '已有' }
  const run: Run = { id: 'run', session_id: 'existing', project_id: 'app', status: 'completed', created_at: '2026-10-18T00:02:00Z', updated_at: '2026-10-18T00:02:00Z', result: { output: 'ok' }, interrupt_id: null, prompt: '追加' }
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method || 'GET'
    calls.push(`${method} ${url}`)
    if (url.endsWith('/health')) return new Response(JSON.stringify({ status: 'ok' }))
    if (url.endsWith('/workspaces')) return new Response(JSON.stringify({ workspaces: [{ id: 'app', path: '/repo' }] }))
    if (method === 'GET' && url.endsWith('/sessions')) return new Response(JSON.stringify({ sessions: [existing] }))
    if (url.includes('/active-run')) return new Response(JSON.stringify({ run: null }))
    if (method === 'GET' && url.includes('/sessions/existing/runs')) return new Response(JSON.stringify({ runs: [], next_cursor: null }))
    if (method === 'POST' && url.includes('/sessions/existing/runs')) return new Response(JSON.stringify(run), { status: 202 })
    if (url.includes('/events')) return new Response('')
    if (method === 'GET' && url.includes('/runs/run')) return new Response(JSON.stringify(run))
    return new Response(JSON.stringify({ detail: 'unexpected' }), { status: 404 })
  }))
  const store = await import('../store/app')
  stop = store.stopStreams
  expect(await store.sendFromHome('追加')).toBe(true)
  expect(store.app.sessionId).toBe('existing')
  expect(calls.some((call) => call === 'POST /api/agent/sessions')).toBe(false)
  expect(calls.some((call) => call.startsWith('POST /api/agent/sessions/existing/runs'))).toBe(true)
})

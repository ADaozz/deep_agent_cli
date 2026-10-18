import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, endpoint, streamEvents } from '../api/client'
import { applySseMessage, emptyView, type RunRecord } from '../lib/runView'
import { renderMarkdown } from '../lib/markdown'
import type { Run } from '../api/types'

const config = { baseUrl: '/api/agent', token: 'legacy-must-not-leak' }
const run: Run = { id: 'run', session_id: 'session', project_id: 'app', status: 'completed', created_at: '2026-10-18T00:00:00Z', updated_at: '2026-10-18T00:01:00Z', result: { output: 'done' }, interrupt_id: null }
function record(): RunRecord { return { id: run.id, sessionId: run.session_id, prompt: '', resume: false, createdAt: run.created_at, cursor: '0', run: { ...run }, view: emptyView() } }
afterEach(() => vi.unstubAllGlobals())

describe('current gateway contract', () => {
  it('maps only same-origin endpoints, never sends the service token, and sends JSON for bodyless actions', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(run)))
    vi.stubGlobal('fetch', fetch)
    await api.cancelRun(config, run.id)
    const [url, options] = fetch.mock.calls[0]
    expect(url).toBe('/api/agent/runs/run/cancel')
    expect(options.body).toBe('{}')
    expect(options.headers).not.toHaveProperty('Authorization')
    expect(options.headers['Content-Type']).toBe('application/json')
    expect(() => endpoint({ baseUrl: 'https://outside.test' }, '/v1/projects')).toThrow()
  })
  it('buffers UTF-8 and CRLF frames across chunk boundaries and ignores unfinished events', async () => {
    const bytes = new TextEncoder().encode(': keepalive\r\n\r\nid: 9007199254740993\r\nevent: delta\r\ndata: {"text":"你好"}\r\n\r\nid: 9007199254740995\ndata: {"unfinished":true}')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({ start(c) { for (let i = 0; i < bytes.length; i += 2) c.enqueue(bytes.slice(i, i + 2)); c.close() } }))))
    const events = []
    for await (const event of streamEvents(config, run.id, '0', new AbortController().signal)) events.push(event)
    expect(events).toEqual([{ id: '9007199254740993', event: 'delta', data: { text: '你好' } }])
  })
  it('replays old events without rolling a completed run back to running', () => {
    const r = record()
    applySseMessage(r, { id: '1', event: 'run_started', data: { ...run, status: 'running', updated_at: run.created_at } })
    expect(r.run.status).toBe('completed')
    applySseMessage(r, { id: '2', event: 'agent_event', data: { type: 'assistant_started' } })
    applySseMessage(r, { id: '3', event: 'agent_event', data: { type: 'assistant_delta', content: 'a' } })
    applySseMessage(r, { id: '4', event: 'agent_event', data: { type: 'assistant_delta', content: 'abc' } })
    expect(r.view.blocks[0].text).toBe('abc')
    applySseMessage(r, { id: '5', event: 'run_finished', data: run })
    expect(r.replayComplete).toBe(true)
    expect(r.view.blocks[0].final).toBe('done')
  })
  it('escapes model HTML and rejects executable markdown URLs', () => {
    const html = renderMarkdown('<img src=x onerror=alert(1)>\n\n[bad](javascript:alert) [ok](https://example.com)\n\n```html\n<script>alert(1)</script>\n```')
    const node = document.createElement('div'); node.innerHTML = html
    expect(node.querySelector('img,script')).toBeNull()
    expect([...node.querySelectorAll('a')].map(a => a.href)).toEqual(['https://example.com/'])
  })
})

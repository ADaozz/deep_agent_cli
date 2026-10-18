import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, endpoint, streamEvents } from '../api/client'
import { applySseMessage, emptyView, type RunRecord } from '../lib/runView'
import { buildTimeline } from '../lib/timeline'
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
  it('keeps inline code nested inside bold', () => {
    const html = renderMarkdown('1. **glob `**/*.py`**：成功')
    expect(html).toContain('<code>**/*.py</code>')
    expect(html).toContain('<strong>glob <code>**/*.py</code></strong>')
    expect(html).not.toMatch(/\u0000/)
    expect(html).not.toContain('>0<')
  })
  it('keeps a closed thinking window when a later reasoning snapshot arrives', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-18T00:00:00Z'))
    const r = record()
    r.run.status = 'running'
    applySseMessage(r, { id: '1', event: 'delta', data: { kind: 'reasoning', text: '先看调用' } })
    vi.setSystemTime(new Date('2026-10-18T00:00:05Z'))
    applySseMessage(r, { id: '2', event: 'delta', data: { kind: 'assistant', text: '完成' } })
    vi.setSystemTime(new Date('2026-10-18T00:00:12Z'))
    applySseMessage(r, { id: '3', event: 'delta', data: { kind: 'reasoning', text: '先看调用（累计快照）' } })
    expect(buildTimeline(r).rows.find((row) => row.kind === 'thinking')?.verb).toBe('思考了 5 秒')
    vi.useRealTimers()
  })
})

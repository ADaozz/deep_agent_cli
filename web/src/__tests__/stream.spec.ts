import { describe, it, expect, vi, afterEach } from 'vitest';
import { streamRun } from '../api/http';
import { applyFrame, newRunView, snapshot } from '../state/reducer';
import type { Run } from '../api/types';
afterEach(() => vi.unstubAllGlobals());
const run: Run = { id: 'r', session_id: 's', project_id: 'p', status: 'completed', created_at: '2026-10-13T00:00:00Z', updated_at: '2026-10-13T00:01:00Z', interrupt_id: null, result: { status: 'completed', output: 'final' } };
describe('transport and authority', () => {
    it('decodes UTF8 split inside a multibyte character without requiring event continuity', async () => {
        const bytes = new TextEncoder().encode('id: 701\nevent: delta\ndata: {"kind":"assistant","text":"中文"}\n\n');
        const fake = vi.fn().mockResolvedValue(new Response(new ReadableStream({ start(controller) { for (let i = 0; i < bytes.length; i++)
                controller.enqueue(bytes.slice(i, i + 1)); controller.close(); } }), { headers: { 'Content-Type': 'text/event-stream' } }));
        vi.stubGlobal('fetch', fake);
        const view = newRunView();
        await streamRun('r', 9, new AbortController().signal, f => applyFrame(view, 'r', f), () => { });
        expect(view.answer).toBe('中文');
        expect(view.cursor).toBe(701);
        expect(fake.mock.calls[0]![0]).toBe('/api/agent/runs/r/events?after=9');
        expect(JSON.stringify(fake.mock.calls[0])).not.toContain('Authorization');
    });
    it('terminal output replaces incrementals and old replay cannot reopen approval', () => {
        const view = newRunView();
        applyFrame(view, 'r', { id: 1, event: 'delta', data: { kind: 'assistant', text: 'partial' } });
        applyFrame(view, 'r', { id: 10, event: 'run_finished', data: run });
        expect(view.answer).toBe('final');
        applyFrame(view, 'r', { id: 11, event: 'interaction_requested', data: { ...run, status: 'waiting_confirmation', updated_at: run.created_at } });
        expect(view.snapshot?.status).toBe('completed');
        expect(view.answer).toBe('final');
    });
    it('keeps tools and event cursors separate across Runs and deduplicates output', () => {
        const a = newRunView(), b = newRunView();
        const frame = { id: 15, event: 'agent_event', data: { type: 'tool_output_delta', tool_call_id: 'same', content: 'stdout' } };
        applyFrame(a, 'a', frame);
        applyFrame(a, 'a', frame);
        applyFrame(b, 'b', { ...frame, id: 20, data: { ...frame.data, content: 'other' } });
        expect(a.tools.same?.stdout).toBe('stdout');
        expect(b.tools.same?.stdout).toBe('other');
        expect(a.cursor).toBe(15);
    });
    it('a mismatched Run snapshot does not advance the cursor', () => { const view = newRunView(); snapshot(view, { ...run, id: 'a' }); expect(() => applyFrame(view, 'a', { id: 5, event: 'run_finished', data: run })).toThrow(); expect(view.cursor).toBe(0); });
});
it('opaque tool IDs do not mutate Object.prototype', () => {
    const view = newRunView();
    applyFrame(view, 'r', { id: 1, event: 'agent_event', data: { type: 'tool_output_delta', tool_call_id: '__proto__', content: 'safe' } });
    expect(view.tools['__proto__']?.stdout).toBe('safe');
    expect(Object.prototype).not.toHaveProperty('stdout');
});

it('an idle transport releases its reader and reports reconcilable connection failure',async()=>{
 let cancelled=false
 vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(new ReadableStream({cancel(){cancelled=true}}))))
 await expect(streamRun('r',0,new AbortController().signal,()=>{},()=>{},10)).rejects.toThrow('长期无响应')
 expect(cancelled).toBe(true)
})

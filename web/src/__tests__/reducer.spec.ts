import { describe, it, expect } from 'vitest';
import { newRunView, applyFrame } from '../state/reducer';
import { SseParser } from '../api/sse';
describe('durable stream reducer', () => {
    it('deduplicates non-contiguous ids and preserves actual cumulative assistant text', () => {
        const view = newRunView();
        applyFrame(view, 'r', { id: 108, event: 'delta', data: { kind: 'assistant', text: '正在' } });
        applyFrame(view, 'r', { id: 115, event: 'delta', data: { kind: 'assistant', text: '正在检查' } });
        applyFrame(view, 'r', { id: 115, event: 'delta', data: { kind: 'assistant', text: 'duplicate' } });
        applyFrame(view, 'r', { id: 121, event: 'agent_event', data: { type: 'assistant_delta', content: 'duplicate' } });
        expect(view.answer).toBe('正在检查');
        expect(view.cursor).toBe(121);
    });
    it('parses split multiline frames and keepalive without inventing a cursor', () => {
        const frames: unknown[] = [];
        const parser = new SseParser(f => frames.push(f));
        parser.push(': keepalive\n\nid: 108\nev');
        parser.push('ent: delta\ndata: {"kind":"assistant",\ndata: "text":"中文"}\n\n');
        expect(frames).toEqual([{ id: 108, event: 'delta', data: { kind: 'assistant', text: '中文' } }]);
    });
    it('does not advance an invalid frame', () => { const view = newRunView(); expect(() => applyFrame(view, 'r', { id: 5, event: 'delta', data: { text: 7 } })).toThrow(); expect(view.cursor).toBe(0); });
});
it('replays the supplied documentation fixture without duplicated text', async () => {
    const text = (await import('../../e2e/fixtures/run-replay.sse?raw')).default;
    const view = newRunView();
    new SseParser(frame => applyFrame(view, 'RUN_TEST_001', frame)).push(text);
    expect(view.answer).toBe('正在检查测试。');
    expect(view.cursor).toBe(121);
});

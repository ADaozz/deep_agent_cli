import { mount } from '@vue/test-utils';
import { describe, it, expect } from 'vitest';
import ChatComposer from '../components/ChatComposer.vue';
import MessageTimeline from '../components/MessageTimeline.vue';
import RunStatus from '../components/RunStatus.vue';
import { newRunView } from '../state/reducer';
describe('accessible workspace controls', () => {
    it('sends Enter, permits Shift+Enter and does not send during IME', async () => {
        const w = mount(ChatComposer, { props: { disabled: false, active: false } });
        const input = w.get('textarea');
        await input.setValue('中文');
        await input.trigger('keydown', { key: 'Enter', shiftKey: true });
        expect(w.emitted('send')).toBeUndefined();
        await input.trigger('compositionstart');
        await input.trigger('keydown', { key: 'Enter' });
        expect(w.emitted('send')).toBeUndefined();
        await input.trigger('compositionend');
        await input.trigger('keydown', { key: 'Enter' });
        expect(w.emitted('send')).toEqual([['中文']]);
    });
    it('blocks sending for an active task', () => { const w = mount(ChatComposer, { props: { disabled: true, active: true } }); expect(w.get('textarea').attributes('disabled')).toBeDefined(); expect(w.text()).toContain('不支持队列'); });
    it.each(['running', 'waiting_confirmation', 'waiting_human', 'paused', 'cancelled', 'failed', 'interrupted'])('renders actual status %s', status => { const w = mount(RunStatus, { props: { status } }); expect(w.get('[role="status"]').attributes('data-status')).toBe(status); });
    it('escapes untrusted prompt and answer', () => { const view = newRunView(); view.snapshot = { id: 'r', session_id: 's', project_id: 'p', status: 'running', created_at: '1', updated_at: '1', result: {}, interrupt_id: null }; view.answer = '<script>unsafe()</script>'; const w = mount(MessageTimeline, { props: { view, prompt: '<img src=x onerror=unsafe()>' } }); expect(w.find('img').exists()).toBe(false); expect(w.find('script').exists()).toBe(false); expect(w.text()).toContain('<script>'); });
});

import { it, expect, vi } from 'vitest';
import { watchEffect, nextTick } from 'vue';
import { mount, flushPromises } from '@vue/test-utils';
import WorkspaceView from '../components/WorkspaceView.vue';
import { useWorkspace } from '../state/workspace';
import { api, streamRun } from '../api/http';
import type { Run, RunSseFrame } from '../api/types';
vi.mock('../api/http', async (importOriginal) => { const actual = await importOriginal<typeof import('../api/http')>(); return { ...actual, api: { ...actual.api, getRun: vi.fn() }, streamRun: vi.fn() }; });
it('uses reactive stream state and cannot leak old Run events after switching', async () => {
    const base: Run = { id: 'a', project_id: 'p', session_id: 's', status: 'running', created_at: '1', updated_at: '1', result: {}, interrupt_id: null };
    const callbacks: Record<string, (frame: RunSseFrame) => void> = {};
    vi.mocked(api.getRun).mockImplementation(async (id) => ({ ...base, id }));
    vi.mocked(streamRun).mockImplementation(async (id, _after, signal, receive, opened) => { callbacks[id] = receive; opened(); await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true })); });
    const workspace = useWorkspace();
    workspace.state.session = 's';
    workspace.state.project = 'p';
    let visible = '';
    const dispose = watchEffect(() => { visible = workspace.view.value?.answer || ''; });
    try {
        await workspace.selectRun('a');
        await nextTick();
        callbacks.a!({ id: 101, event: 'delta', data: { kind: 'assistant', text: 'first' } });
        await nextTick();
        expect(visible).toBe('first');
        await workspace.selectRun('b');
        await nextTick();
        callbacks.a!({ id: 102, event: 'delta', data: { kind: 'assistant', text: 'late old frame' } });
        callbacks.b!({ id: 201, event: 'delta', data: { kind: 'assistant', text: 'second' } });
        await nextTick();
        expect(visible).toBe('second');
        expect(workspace.view.value?.cursor).toBe(201);
    }
    finally {
        workspace.stop();
        dispose();
    }
});

it('keeps input unavailable until restored history selection is stable, then preserves the draft', async () => {
    localStorage.setItem('deep-agent.selection', JSON.stringify({ project: 'p', session: 's', run: 'selected' }));
    const base: Run = { id: 'latest', project_id: 'p', session_id: 's', status: 'completed', created_at: '1', updated_at: '1', result: {}, interrupt_id: null };
    vi.spyOn(api, 'projects').mockResolvedValue({ projects: [{ id: 'p' }] });
    vi.spyOn(api, 'health').mockResolvedValue({ status: 'ok' });
    vi.spyOn(api, 'sessions').mockResolvedValue({ sessions: [{ id: 's', project_id: 'p', created_at: '1' }] });
    vi.spyOn(api, 'activeRun').mockResolvedValue({ run: null });
    vi.spyOn(api, 'codeCheckpoint').mockImplementation(async id => ({ run_id: id, status: 'unavailable', available: false, files: [] }));
    let complete!: () => void;
    const history = { runs: [base], next_cursor: null };
    vi.spyOn(api, 'history').mockImplementationOnce(() => new Promise(resolve => { complete = () => resolve(history); })).mockResolvedValue(history);
    vi.mocked(api.getRun).mockImplementation(async id => ({ ...base, id }));
    vi.mocked(streamRun).mockResolvedValue(undefined);
    const wrapper = mount(WorkspaceView);
    try {
        await flushPromises();
        expect(wrapper.get('textarea').attributes('disabled')).toBeDefined();
        complete();
        await flushPromises();
        expect(wrapper.get('textarea').attributes('disabled')).toBeUndefined();
        expect((wrapper.get('[aria-label="选择任务"]').element as HTMLSelectElement).value).toBe('selected');
        await wrapper.get('textarea').setValue('不会被历史恢复清空');
        await flushPromises();
        expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('不会被历史恢复清空');
    } finally {
        wrapper.unmount();
        vi.restoreAllMocks();
        localStorage.clear();
    }
});

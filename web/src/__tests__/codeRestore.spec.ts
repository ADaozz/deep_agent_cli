import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import CodeRestoreControls from '../components/CodeRestoreControls.vue';
import { api, ApiError } from '../api/http';
import type { CodeCheckpoint, Run } from '../api/types';

vi.mock('../api/http', async (original) => {
    const actual = await original<typeof import('../api/http')>();
    return { ...actual, api: { ...actual.api, codeCheckpoint: vi.fn(), restoreCode: vi.fn() } };
});
const run: Run = { id: 'r', session_id: 's', project_id: 'p', status: 'completed', created_at: '1', updated_at: '1', result: {}, interrupt_id: null };
const checkpoint: CodeCheckpoint = { run_id: 'r', available: true, status: 'ready', revision: 'one', files: [{ path: 'file.ts', kind: 'file', change: 'modify' }], can_undo: true, can_redo: false };
const mountControls = (props = {}) => mount(CodeRestoreControls, { props: { run, busy: false, projectBusy: false, ...props } });
function button(wrapper: ReturnType<typeof mountControls>, name: string) {
    const result = wrapper.findAll('button').find(item => item.text() === name);
    if (!result) throw new Error('Missing button: ' + name);
    return result;
}
beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.codeCheckpoint).mockResolvedValue(checkpoint);
});

describe('explicit per-round code undo/redo', () => {
    it('requires confirmation, uses the current revision and allows undoing the undo', async () => {
        const wrapper = mountControls();
        await flushPromises();
        await button(wrapper, '回退本轮代码').trigger('click');
        expect(api.restoreCode).not.toHaveBeenCalled();
        vi.mocked(api.restoreCode).mockResolvedValue({ ...checkpoint, status: 'reverted', revision: 'two', can_undo: false, can_redo: true });
        await button(wrapper, '确认回退本轮').trigger('click');
        await flushPromises();
        expect(api.restoreCode).toHaveBeenLastCalledWith('r', 'undo', 'one');
        expect(wrapper.text()).toContain('本轮代码已回退');
        await button(wrapper, '撤销回退').trigger('click');
        vi.mocked(api.restoreCode).mockResolvedValue({ ...checkpoint, revision: 'three' });
        await button(wrapper, '确认撤销回退').trigger('click');
        await flushPromises();
        expect(api.restoreCode).toHaveBeenLastCalledWith('r', 'redo', 'two');
        expect(wrapper.emitted('busy')).toEqual([[true], [false], [true], [false]]);
        expect(wrapper.emitted('changed')).toHaveLength(2);
    });
    it('does not restore when cancelling confirmation', async () => {
        const wrapper = mountControls();
        await flushPromises();
        await button(wrapper, '回退本轮代码').trigger('click');
        await button(wrapper, '取消').trigger('click');
        expect(api.restoreCode).not.toHaveBeenCalled();
        expect(wrapper.text()).not.toContain('恢复到本轮开始前的代码？');
    });
    it('shows 409 conflicts, reconciles and never automatically retries a write', async () => {
        const wrapper = mountControls();
        await flushPromises();
        vi.mocked(api.restoreCode).mockRejectedValue(new ApiError(409, '文件已被后续轮次修改'));
        await button(wrapper, '回退本轮代码').trigger('click');
        await button(wrapper, '确认回退本轮').trigger('click');
        await flushPromises();
        expect(api.restoreCode).toHaveBeenCalledTimes(1);
        expect(wrapper.get('[role="alert"]').text()).toContain('409');
        expect(api.codeCheckpoint).toHaveBeenCalledTimes(2);
    });
    it.each([{ projectBusy: true }, { run: { ...run, status: 'waiting_confirmation' } }])('cannot modify code while a project is active: %j', async props => {
        const wrapper = mountControls(props);
        await flushPromises();
        expect(button(wrapper, '回退本轮代码').attributes('disabled')).toBeDefined();
        expect(api.restoreCode).not.toHaveBeenCalled();
    });
    it('does not pretend old rounds have restorable snapshots', async () => {
        vi.mocked(api.codeCheckpoint).mockResolvedValue({ run_id: 'r', status: 'unavailable', available: false, reason: '旧轮次没有代码快照', files: [] });
        const wrapper = mountControls();
        await flushPromises();
        expect(wrapper.text()).toContain('旧轮次没有代码快照');
        expect(wrapper.findAll('button')).toHaveLength(0);
    });
    it('ignores an old response after selecting another round', async () => {
        let resolve!: (value: CodeCheckpoint) => void;
        vi.mocked(api.codeCheckpoint).mockImplementation(id => id === 'r' ? new Promise(done => { resolve = done; }) : Promise.resolve({ ...checkpoint, run_id: id, revision: 'new-round' }));
        const wrapper = mountControls();
        await wrapper.setProps({ run: { ...run, id: 'second' } });
        await flushPromises();
        resolve({ ...checkpoint, status: 'reverted', can_undo: false, can_redo: true });
        await flushPromises();
        expect(wrapper.text()).not.toContain('本轮代码已回退');
        await button(wrapper, '回退本轮代码').trigger('click');
        vi.mocked(api.restoreCode).mockResolvedValue({ ...checkpoint, run_id: 'second' });
        await button(wrapper, '确认回退本轮').trigger('click');
        await flushPromises();
        expect(api.restoreCode).toHaveBeenLastCalledWith('second', 'undo', 'new-round');
    });
});

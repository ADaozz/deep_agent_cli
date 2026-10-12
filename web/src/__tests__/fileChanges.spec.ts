import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';
import CodeChangesPanel from '../components/CodeChangesPanel.vue';
import InspectorPanel from '../components/InspectorPanel.vue';
import { fileChanges } from '../state/fileChanges';
import { applyFrame, newRunView, type ToolView } from '../state/reducer';

function tool(overrides: Partial<ToolView> = {}): ToolView {
    return { id: 'write', name: 'write_file', arguments: { file_path: '/workspace/file.ts', content: 'new\n' },
        status: 'completed', stdout: '', stderr: '', result: 'written', ...overrides };
}

describe('file-tool change records', () => {
    it('preserves authoritative write classification through SSE replay without duplication', () => {
        const view = newRunView();
        const started = { id: 1, event: 'agent_event', data: { type: 'tool_started', tool_call_id: 'write', name: 'write_file', arguments: tool().arguments } };
        const finished = { id: 4, event: 'agent_event', data: { type: 'tool_completed', tool_call_id: 'write', name: 'write_file', artifact: { operation: 'create' }, is_error: false } };
        applyFrame(view, 'r', started);
        expect(fileChanges(view.tools)).toEqual([]);
        applyFrame(view, 'r', finished);
        applyFrame(view, 'r', finished);
        expect(fileChanges(view.tools)).toMatchObject([{ id: 'write', operation: 'create', after: 'new\n' }]);
    });
    it.each(['failed', 'running'])('excludes %s calls including rejected tools', status => {
        expect(fileChanges({ write: tool({ status, artifact: { operation: 'create' } }) })).toEqual([]);
    });
    it('does not turn shell results or guessed model text into file changes', () => {
        expect(fileChanges({ shell: tool({ name: 'execute', result: '1 file changed, 42 insertions', artifact: { operation: 'create' } }) })).toEqual([]);
    });
    it('distinguishes overwrite from unclassified writes without inventing previous contents', () => {
        expect(fileChanges({ a: tool({ artifact: { operation: 'overwrite' } }), b: tool({ id: 'unknown' }) })
            .map(c => [c.operation, c.before])).toEqual([['overwrite', undefined], ['write', undefined]]);
    });
    it('keeps empty edit sides and replacement scope; skips missing target paths', () => {
        const edit = tool({ name: 'edit_file', arguments: { file_path: '/workspace/file.ts', old_string: '', new_string: 'added', replace_all: true } });
        expect(fileChanges({ edit })).toMatchObject([{ operation: 'edit', before: '', after: 'added', replaceAll: true }]);
        expect(fileChanges({ edit: tool({ arguments: { content: 'missing path' } }) })).toEqual([]);
    });
    it('counts unique files separately from operations and escapes paths and contents', () => {
        const writes = { a: tool({ id: 'a', arguments: { file_path: '<img src=x>', content: '<script>unsafe()</script>' }, artifact: { operation: 'create' } }),
            b: tool({ id: 'b', name: 'delete', arguments: { file_path: '<img src=x>' } }) };
        const wrapper = mount(CodeChangesPanel, { props: { tools: writes } });
        expect(wrapper.text()).toContain('1 个文件 · 2 次成功操作');
        expect(wrapper.text()).toContain('<script>unsafe()</script>');
        expect(wrapper.find('script').exists()).toBe(false);
        expect(wrapper.find('img').exists()).toBe(false);
        expect(wrapper.text()).toContain('删除前内容未记录');
    });
    it('resets the artifact display when the selected Run changes', async () => {
        const view = newRunView();
        view.tools.write = tool();
        const wrapper = mount(InspectorPanel, { props: { view } });
        expect(wrapper.find('.file-change').exists()).toBe(true);
        await wrapper.setProps({ view: newRunView() });
        expect(wrapper.find('.file-change').exists()).toBe(false);
        expect(wrapper.text()).toContain('暂无成功的文件工具变更记录');
    });
});

import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import { humanValues, approvalBody } from '../state/forms';
import ToolApprovalPanel from '../components/ToolApprovalPanel.vue';
import HumanInputForm from '../components/HumanInputForm.vue';
import type { HumanInputRequest } from '../api/types';
const request: HumanInputRequest = { type: 'human_input', interactionType: 'clarification', title: 'Question', reason: 'reason', question: 'answer', blocking: true, impact: [], fields: [{ id: 'flag', type: 'boolean', label: '确认', required: true, placeholder: '', options: [] }, { id: 'notes', type: 'textarea', label: '说明', required: false, placeholder: '', options: [] }] };
const pending = [{ toolCallId: 'a', name: 'execute', args: { command: 'pytest -q', network: false } }, { toolCallId: 'b', name: 'write_file', args: { file_path: 'file.txt' } }];
describe('explicit interactions', () => {
    it('requires every tool decision and maps camelCase to service keys', () => {
        expect(() => approvalBody(pending, { a: 'approve' })).toThrow();
        expect(approvalBody(pending, { a: 'approve', b: 'reject' })).toEqual([{ tool_call_id: 'a', decision: 'approve' }, { tool_call_id: 'b', decision: 'reject' }]);
    });
    it('makes one batch emission and resets on a new interaction', async () => {
        const w = mount(ToolApprovalPanel, { props: { calls: pending, interruptId: 'old', project: 'p', busy: false } });
        expect(w.get('.approval-footer button').attributes('disabled')).toBeDefined();
        await w.findAll('.approval-decisions')[0]!.findAll('button')[1]!.trigger('click');
        await w.findAll('.approval-decisions')[1]!.findAll('button')[0]!.trigger('click');
        await w.get('.approval-footer button').trigger('click');
        expect(w.emitted('submit')).toEqual([[{ interrupt_id: 'old', decisions: [{ tool_call_id: 'a', decision: 'approve' }, { tool_call_id: 'b', decision: 'reject' }] }]]);
        await w.setProps({ interruptId: 'new' });
        expect(w.get('.approval-footer button').attributes('disabled')).toBeDefined();
    });
    it.each([undefined, null, '', ' \n', []])('rejects empty required input %s', value => expect(() => humanValues(request, { flag: value }, {})).toThrow());
    it('accepts false and does not require optional fields', () => expect(humanValues(request, { flag: false }, {})).toEqual({ flag: false }));
    it('encodes Other as actual free text, not a sentinel', () => { const form = { ...request, fields: [{ id: 'pick', type: 'multi_select' as const, label: '选择', required: true, placeholder: '', options: [{ value: '__other__', label: 'Other', description: '', allowsCustomText: true }] }] }; expect(humanValues(form, { pick: ['existing', '__other__'] }, { pick: 'custom choice' })).toEqual({ pick: ['existing', 'custom choice'] }); });
    it('renders boolean unanswered initially and emits false', async () => { const w = mount(HumanInputForm, { props: { request, interruptId: 'i', busy: false } }); await w.get('select').setValue('false'); await w.get('form').trigger('submit'); expect(w.emitted('submit')).toEqual([[{ interrupt_id: 'i', values: { flag: false } }]]); });
    it('does not invent an unsupported field implementation', () => { const bad = { ...request, fields: [{ ...request.fields[0]!, type: 'future' as 'boolean' }] }; const w = mount(HumanInputForm, { props: { request: bad, interruptId: 'i', busy: false } }); expect(w.get('button').attributes('disabled')).toBeDefined(); });
});
it('opaque tool IDs cannot inherit a decision from Object.prototype', () => {
    const calls = [{ toolCallId: 'toString', name: 'execute', args: {} }];
    expect(() => approvalBody(calls, {})).toThrow();
});

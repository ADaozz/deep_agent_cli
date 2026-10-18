import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import InlineInteraction from '../components/InlineInteraction.vue'
import type { InteractionModel } from '../lib/runView'
const base: InteractionModel = { kind: 'human', runId: 'r', interruptId: 'i', status: 'waiting_human', pendingTools: [], humanInput: { type: 'human_input', interactionType: 'clarification', title: '', reason: '', question: '', blocking: true, impact: [], fields: [
  { id: 'notes', label: '说明', type: 'textarea', required: false, placeholder: '', options: [] },
  { id: 'confirm', label: '确认', type: 'boolean', required: true, placeholder: '', options: [] },
  { id: 'checks', label: '检查', type: 'multi_select', required: true, placeholder: '', options: [{ value: 'unit', label: '单测', description: '' }, { value: 'CUSTOM', label: '其他', description: '', allowsCustomText: true }] },
] } }
describe('inline service interactions', () => {
  it('sends false, multiline text and custom multi-select alongside declared choices', async () => {
    const w = mount(InlineInteraction, { props: { interaction: base, busy: false, submitted: false, rejected: '' } })
    await w.get('textarea').setValue('one\ntwo')
    const click = async (label: string) => { await w.findAll('button').find(b => b.text() === label)!.trigger('click') }
    await click('否'); await click('单测'); await click('其他')
    await w.get('input[placeholder="自定义答案"]').setValue('custom-check')
    await click('发送')
    expect(w.emitted('human')?.[0]).toEqual([{ notes: 'one\ntwo', confirm: false, checks: ['unit', 'custom-check'] }])
  })
  it('requires every batch decision and locks forms without a current interrupt ID', async () => {
    const interaction = { ...base, kind: 'approvals' as const, pendingTools: [{ toolCallId: 'a', name: 'execute', args: {} }, { toolCallId: 'b', name: 'write_file', args: {} }] }
    const w = mount(InlineInteraction, { props: { interaction, busy: false, submitted: false, rejected: '' } })
    await w.get('[aria-label="批准 execute"]').trigger('click')
    expect(w.emitted('approve')).toBeUndefined()
    await w.get('[aria-label="拒绝 write_file"]').trigger('click')
    await w.findAll('button').find(b => b.text() === '提交决定')!.trigger('click')
    expect(w.emitted('approve')?.[0]).toEqual([[{ tool_call_id: 'a', decision: 'approve' }, { tool_call_id: 'b', decision: 'reject' }]])
    await w.setProps({ interaction: { ...interaction, interruptId: null } })
    expect(w.findAll('button').every(b => b.attributes('disabled') !== undefined)).toBe(true)
  })
})

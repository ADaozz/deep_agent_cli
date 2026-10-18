import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ModelPicker from '../components/ModelPicker.vue'
import { api, ApiError } from '../api/client'
import { models } from '../store/models'
import type { ModelCatalog } from '../api/types'

const catalog: ModelCatalog = { models: [
  { id: 'OrgA/one', model: 'model-one', source: 'OrgA', reasoning_efforts: ['low', 'high'] },
  { id: 'OrgB/two', model: 'model-two', source: 'OrgB', reasoning_efforts: ['default'] },
], current: { model_id: 'OrgA/one', model: 'model-one', source: 'OrgA', reasoning_effort: 'low' } }
const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => {
  models.catalogs = {}; models.sessions = {}; models.drafts = {}; models.changing = {}
  vi.spyOn(api, 'models').mockResolvedValue(catalog)
  vi.spyOn(api, 'sessionModel').mockResolvedValue(catalog.current)
})
afterEach(() => { wrappers.forEach(w => w.unmount()); wrappers.length = 0; vi.restoreAllMocks() })
async function setup(sessionId = 's') {
  const w = mount(ModelPicker, { props: { projectId: 'app', sessionId, disabled: false } }); wrappers.push(w)
  await flushPromises()
  const click = async (text: string) => { await w.findAll('.popover button').find(b => b.text() === text)!.trigger('click'); await flushPromises() }
  return { w, click }
}
describe('CLI-style model picker', () => {
  it('selects organization, model and effort in order; applies a real session change and shows only the model name', async () => {
    const change = vi.spyOn(api, 'selectModel').mockResolvedValue({ model_id: 'OrgB/two', model: 'model-two', source: 'OrgB', reasoning_effort: 'default' })
    const { w, click } = await setup()
    expect(w.get('.model-pick').text()).toBe('model-one')
    await w.get('.model-pick').trigger('click'); await flushPromises()
    expect(w.text()).toContain('选择组织')
    await click('OrgB')
    expect(w.text()).toContain('选择模型名称')
    expect(w.text()).not.toContain('model-oneOrgA')
    await click('model-two')
    expect(w.text()).toContain('选择思考强度')
    expect(change).not.toHaveBeenCalled()
    await click('默认')
    expect(change).toHaveBeenCalledWith({ baseUrl: '/api/agent' }, 's', 'OrgB/two', 'default')
    expect(w.get('.model-pick').text()).toBe('model-two')
    expect(w.find('.popover').exists()).toBe(false)
  })
  it('supports back/cancel and home drafts without changing the running service', async () => {
    const change = vi.spyOn(api, 'selectModel')
    const { w, click } = await setup('')
    await w.get('.model-pick').trigger('click'); await flushPromises()
    await click('OrgA'); await click('model-one')
    await w.get('[aria-label="上一步"]').trigger('click')
    expect(w.text()).toContain('选择模型名称')
    expect(change).not.toHaveBeenCalled()
    await click('model-one'); await click('high')
    expect(models.drafts.app).toEqual({ ...catalog.current, reasoning_effort: 'high' })
    expect(change).not.toHaveBeenCalled()
    await w.setProps({ disabled: true })
    expect(w.get('.model-pick').attributes('disabled')).toBeDefined()
  })
  it('keeps the actual current model when the service rejects a switch', async () => {
    vi.spyOn(api, 'selectModel').mockRejectedValue(new ApiError(409, 'Finish current run'))
    const { w, click } = await setup()
    await w.get('.model-pick').trigger('click'); await flushPromises()
    await click('OrgB'); await click('model-two'); await click('默认')
    expect(w.get('[role="alert"]').text()).toContain('409')
    expect(w.get('.model-pick').text()).toBe('model-one')
  })
})

it('uses the configured default even when it is not the first model in the catalog', async () => {
  const change = vi.spyOn(api, 'selectModel')
  vi.mocked(api.models).mockResolvedValue({ ...catalog, current: {model_id:'OrgB/two',model:'model-two',source:'OrgB',reasoning_effort:'default'} })
  const { w } = await setup('')
  expect(w.get('.model-pick').text()).toBe('model-two')
  expect(change).not.toHaveBeenCalled()
})

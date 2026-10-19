import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import AgentSidebar from '../components/AgentSidebar.vue'
import { app } from '../store/app'
import { appearance, setAppearance } from '../store/appearance'
import { makeRunRecord } from '../store/records'

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date(2026, 9, 19, 12))
  app.sessions = []
  app.records = {}
  setAppearance('light')
})
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); setAppearance('light'); document.body.innerHTML = '' })

function session(id: string, daysAgo: number): void {
  const date = new Date(2026, 9, 19 - daysAgo, 10).toISOString()
  const value = { id, project_id: 'app', created_at: date }
  app.sessions.push(value)
  app.records[id] = { session: value, title: id, runs: [], draft: '' }
}

it('groups sessions by last activity into today, previous seven days and earlier, with independent collapse controls', async () => {
  session('today', 0); session('yesterday', 1); session('week', 7); session('old', 8); session('resumed', 20)
  const now = new Date(2026, 9, 19, 11).toISOString()
  app.records.resumed.runs = [makeRunRecord({ id: 'r', session_id: 'resumed', project_id: 'app', status: 'completed', created_at: now, updated_at: now, result: {}, interrupt_id: null }, '', false)]
  const sidebar = mount(AgentSidebar, { attachTo: document.body })
  expect(sidebar.findAll('.side-group').map(group => group.text())).toEqual(['今天', '最近七天', '更早'])
  expect(sidebar.findAll('.side-list').map(list => list.findAll('.chat-title').map(title => title.text()))).toEqual([['resumed', 'today'], ['yesterday', 'week'], ['old']])
  expect(sidebar.find('[title="筛选会话"]').exists()).toBe(false)
  const buttons = sidebar.findAll('.side-group')
  for (const [index, button] of buttons.entries()) {
    expect(button.attributes('aria-expanded')).toBe('true')
    await button.trigger('click')
    expect(button.attributes('aria-expanded')).toBe('false')
    expect(sidebar.findAll('.side-list')[index]!.isVisible()).toBe(false)
  }
  await buttons[1]!.trigger('click')
  expect(sidebar.findAll('.side-list')[1]!.isVisible()).toBe(true)
  expect(sidebar.findAll('.side-list')[0]!.isVisible()).toBe(false)
  expect(sidebar.findAll('.side-list')[2]!.isVisible()).toBe(false)
  sidebar.unmount()
})

it('changes appearance from the More submenu and persists the preference', async () => {
  const sidebar = mount(AgentSidebar)
  await sidebar.get('[title="更多"]').trigger('click')
  await sidebar.findAll('button').find(button => button.text() === '外观浅色')!.trigger('click')
  const choices = sidebar.findAll('[role="menuitemradio"]')
  expect(choices.map(choice => choice.text())).toEqual(['浅色', '深色'])
  expect(choices[0]!.attributes('aria-checked')).toBe('true')
  await choices[1]!.trigger('click'); await flushPromises()
  expect(appearance.value).toBe('dark')
  expect(document.documentElement.dataset.theme).toBe('dark')
  expect(localStorage.getItem('deepagent.appearance.v1')).toBe('dark')
  expect(sidebar.find('.popover').exists()).toBe(false)
  await sidebar.get('[title="更多"]').trigger('click')
  expect(sidebar.text()).toContain('外观深色')
  await sidebar.findAll('button').find(button => button.text() === '外观深色')!.trigger('click')
  await sidebar.findAll('[role="menuitemradio"]')[0]!.trigger('click')
  expect(document.documentElement.dataset.theme).toBe('light')
  sidebar.unmount()
})

it('still changes the appearance when browser storage is unavailable', () => {
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
  expect(() => setAppearance('dark')).not.toThrow()
  expect(document.documentElement.dataset.theme).toBe('dark')
})

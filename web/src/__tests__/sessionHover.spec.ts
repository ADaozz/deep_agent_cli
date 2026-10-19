import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import AgentSidebar from '../components/AgentSidebar.vue'
import { app } from '../store/app'
import { workspaceGit } from '../store/git'

afterEach(() => { vi.useRealTimers(); document.body.innerHTML = '' })

it('shows only current workspace diff statistics and repository/branch information on hover', async () => {
  vi.useFakeTimers()
  const session = { id: 's', project_id: 'app', created_at: new Date().toISOString() }
  app.sessions = [session]
  app.records = { s: { session, title: '文件差异审阅', runs: [], draft: '' } }
  app.projects = [{ id: 'app', path: '/home/neo/project/deep_agent_cli' }]
  workspaceGit.app = { available: true, changed: 6, added: 43, removed: 19, branch: 'web_beta', upstream: 'origin/web_beta' }
  const sidebar = mount(AgentSidebar, { attachTo: document.body })
  await sidebar.get('.chat-row').trigger('mouseenter')
  await vi.advanceTimersByTimeAsync(250); await flushPromises()
  const card = document.querySelector('[data-sidebar-environment-hover-card]')!
  expect(card.querySelectorAll('.session-hover-line')).toHaveLength(2)
  expect(card.textContent).toContain('6 个文件')
  expect(card.textContent).toContain('+43')
  expect(card.textContent).toContain('-19')
  expect(card.textContent).toContain('deep_agent_cli')
  expect(card.textContent).toContain('web_beta → origin/web_beta')
  expect(card.textContent).not.toContain('文件差异审阅')
  expect(card.textContent).not.toContain('From Website')
  expect(card.querySelector('a')).toBeNull()
  expect(sidebar.get('.chat-row').attributes('aria-describedby')).toBe('session-hover-card')
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); await flushPromises()
  expect(document.querySelector('[data-sidebar-environment-hover-card]')).toBeNull()
  sidebar.unmount()
})

import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SidePanel from '../components/SidePanel.vue'
import { api } from '../api/client'
import type { GitFileDiff, WorkspaceGit } from '../api/types'

const git: WorkspaceGit = {
  available: true,
  head: 'abc',
  files: [
    { path: 'agent/server/gitinfo.py', added: 3, removed: 0, status: 'modified', binary: false, staged: true, unstaged: true, untracked: false, old_path: null },
    { path: 'notes/new.txt', added: null, removed: null, status: 'untracked', binary: false, staged: false, unstaged: false, untracked: true, old_path: null },
  ],
}
const preview: GitFileDiff = {
  available: true,
  path: 'agent/server/gitinfo.py',
  scope: 'all',
  binary: false,
  truncated: true,
  hunks: [{
    header: '@@ -42,1 +42,3 @@',
    old_start: 42,
    old_count: 1,
    new_start: 42,
    new_count: 3,
    lines: [
      { kind: 'add', text: '    timeout = 120', old_no: null, new_no: 42 },
      { kind: 'add', text: '    if cancelled:', old_no: null, new_no: 43 },
      { kind: 'add', text: '        return None', old_no: null, new_no: 44 },
    ],
  }],
}

afterEach(() => vi.restoreAllMocks())

describe('workspace diff panel', () => {
  it('opens a real line diff, switches scope, collapses hunks, and can request the full patch', async () => {
    const request = vi.spyOn(api, 'projectGitDiff').mockResolvedValue(preview)
    vi.spyOn(api, 'projectGit').mockResolvedValue(git)
    const panel = mount(SidePanel, { props: { git, projectId: 'app', title: 'app', subtitle: '' } })
    expect(panel.text()).toContain('agent/server/gitinfo.py')
    expect(panel.text()).not.toContain('notes/new.txt')
    expect(panel.text()).not.toContain('变更文件')
    await panel.get('button[title="agent/server/gitinfo.py"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledWith({ baseUrl: '/api/agent' }, 'app', 'agent/server/gitinfo.py', 'all', false)
    expect(panel.text()).toContain('timeout = 120')
    expect(panel.text()).toContain('if cancelled:')
    await panel.get('.hunk-toggle').trigger('click')
    expect(panel.get('.hunk-toggle').attributes('aria-expanded')).toBe('false')
    expect(panel.get('.hunk-lines').isVisible()).toBe(false)
    request.mockResolvedValue({ ...preview, truncated: false, scope: 'staged' })
    const scopes = panel.findAll('[aria-label="差异范围"] [role="tab"]')
    await scopes[1].trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith({ baseUrl: '/api/agent' }, 'app', 'agent/server/gitinfo.py', 'staged', false)
    expect(panel.text()).not.toContain('notes/new.txt')
    await scopes[2].trigger('click')
    await flushPromises()
    expect(panel.text()).toContain('未跟踪')
    expect(panel.text()).toContain('notes/new.txt')
    request.mockClear()
    request.mockResolvedValue({ ...preview, truncated: false })
    await panel.get('button[title="刷新工作区"]').trigger('click')
    await flushPromises()
    expect(api.projectGit).toHaveBeenCalled()
    expect(request).toHaveBeenCalledWith({ baseUrl: '/api/agent' }, 'app', 'agent/server/gitinfo.py', 'unstaged', false)
  })

  it('requests the uncapped patch when the preview is truncated', async () => {
    const request = vi.spyOn(api, 'projectGitDiff')
    request.mockResolvedValueOnce(preview)
    request.mockResolvedValueOnce({ ...preview, truncated: false, hunks: [...preview.hunks!, { ...preview.hunks![0], header: '@@ -80,1 +80,1 @@', lines: [{ kind: 'add', text: 'rest', old_no: null, new_no: 80 }] }] })
    const panel = mount(SidePanel, { props: { git, projectId: 'app', title: 'app', subtitle: '' } })
    await panel.get('button[title="agent/server/gitinfo.py"]').trigger('click')
    await flushPromises()
    await panel.get('.diff-full').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith({ baseUrl: '/api/agent' }, 'app', 'agent/server/gitinfo.py', 'all', true)
    expect(panel.text()).toContain('rest')
    expect(panel.find('.diff-full').exists()).toBe(false)
  })
})

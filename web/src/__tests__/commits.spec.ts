import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import CommitHistory from '../components/CommitHistory.vue'
import SidePanel from '../components/SidePanel.vue'
import { api } from '../api/client'
import type { GitCommit, GitCommits } from '../api/types'

function commit(sha: string, date = '2026-10-19T08:41:18+08:00'): GitCommit {
  return { sha: sha.repeat(40), subject: '修复工作区锁', message: '修复工作区锁\n\n详细说明',
    author: '测试作者', authored_at: date, committed_at: date, added: 43, removed: 19, files: 6,
    url: `https://github.com/example/repo/commit/${sha.repeat(40)}` }
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('groups real commits by date, renders metadata and links, copies SHA, and loads more', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  const first = commit('a'), second = commit('b'), older = commit('c', '2026-10-18T08:00:00+08:00')
  const request = vi.spyOn(api, 'projectCommits')
    .mockResolvedValueOnce({ available: true, commits: [first, second], next_cursor: 'page2' })
    .mockResolvedValueOnce({ available: true, commits: [older], next_cursor: null })
  const panel = mount(CommitHistory, { props: { projectId: 'app', head: first.sha } })
  await flushPromises()
  expect(panel.findAll('.commit-day')).toHaveLength(1)
  expect(panel.findAll('.commit-row')).toHaveLength(2)
  expect(panel.get('.commit-subject').attributes('href')).toBe(first.url)
  expect(panel.get('.commit-subject').attributes('title')).toContain('详细说明')
  expect(panel.get('.diff-added').text()).toBe('+43')
  expect(panel.get('.diff-removed').text()).toBe('-19')
  expect(panel.get('.commit-files').text()).toBe('6 个文件')
  await panel.get('[aria-label="复制提交 SHA aaaaaaa"]').trigger('click')
  expect(writeText).toHaveBeenCalledWith(first.sha)
  await panel.get('.commit-more').trigger('click'); await flushPromises()
  expect(request).toHaveBeenLastCalledWith({ baseUrl: '/api/agent' }, 'app', 'page2')
  expect(panel.findAll('.commit-day')).toHaveLength(2)
  expect(panel.findAll('.commit-row')).toHaveLength(3)
  expect(panel.find('.commit-more').exists()).toBe(false)
  panel.unmount()
})

it('ignores a stale project response and displays local-only commits without links', async () => {
  let resolveOld!: (value: GitCommits) => void
  vi.spyOn(api, 'projectCommits')
    .mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    .mockResolvedValueOnce({ available: true, commits: [{ ...commit('b'), subject: '新工作区', url: null }], next_cursor: null })
  const panel = mount(CommitHistory, { props: { projectId: 'old', head: 'a' } })
  await panel.setProps({ projectId: 'new', head: 'b' }); await flushPromises()
  resolveOld({ available: true, commits: [{ ...commit('a'), subject: '旧工作区' }], next_cursor: null })
  await flushPromises()
  expect(panel.text()).toContain('新工作区')
  expect(panel.text()).not.toContain('旧工作区')
  expect(panel.find('a').exists()).toBe(false)
  panel.unmount()
})

it('shows an empty history and retries a failed request', async () => {
  vi.spyOn(api, 'projectCommits').mockRejectedValueOnce(new Error('连接失败'))
    .mockResolvedValueOnce({ available: true, commits: [], next_cursor: null })
  const panel = mount(CommitHistory, { props: { projectId: 'app' } })
  await flushPromises()
  expect(panel.get('[role="alert"]').text()).toBe('连接失败')
  await panel.get('.commit-error button').trigger('click'); await flushPromises()
  expect(panel.text()).toContain('暂无提交记录')
  panel.unmount()
})

it('loads commits only when the commits tab opens', async () => {
  const request = vi.spyOn(api, 'projectCommits').mockResolvedValue({ available: true, commits: [commit('a')], next_cursor: null })
  const panel = mount(SidePanel, { props: { projectId: 'app', git: { available: true, head: 'a', files: [] } } })
  expect(request).not.toHaveBeenCalled()
  await panel.findAll('button').find(button => button.text() === '提交')!.trigger('click'); await flushPromises()
  expect(panel.text()).toContain('修复工作区锁')
  expect(request).toHaveBeenCalledOnce()
  panel.unmount()
})

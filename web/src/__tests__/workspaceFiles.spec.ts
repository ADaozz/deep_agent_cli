import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import WorkspaceFiles from '../components/WorkspaceFiles.vue'
import SidePanel from '../components/SidePanel.vue'
import { api } from '../api/client'
import { invalidateWorkspaceFiles, workspaceFileVersions } from '../store/files'
import type { WorkspaceDirectory } from '../api/types'

const root: WorkspaceDirectory = { path: '', entries: [
  { name: 'src', path: 'src', kind: 'directory' },
  { name: 'README.md', path: 'README.md', kind: 'file' },
], next_offset: null }
const nested: WorkspaceDirectory = { path: 'src', entries: [{ name: 'app.ts', path: 'src/app.ts', kind: 'file' }], next_offset: null }
beforeEach(() => { workspaceFileVersions.app = 0 })
afterEach(() => vi.restoreAllMocks())

it('lazily expands directories and displays escaped, read-only file contents', async () => {
  const list = vi.spyOn(api, 'workspaceFiles').mockImplementation(async (_config, _project, path) => path ? nested : root)
  const preview = vi.spyOn(api, 'previewFile').mockResolvedValue({ path: 'src/app.ts', size: 25, kind: 'text', truncated: false, content: '<script>alert(1)</script>' })
  const panel = mount(WorkspaceFiles, { props: { projectId: 'app' } })
  await flushPromises()
  expect(list).toHaveBeenCalledTimes(1)
  expect(panel.find('[title="src/app.ts"]').exists()).toBe(false)
  await panel.get('[title="src"]').trigger('click'); await flushPromises()
  expect(list).toHaveBeenLastCalledWith({ baseUrl: '/api/agent' }, 'app', 'src', 0)
  await panel.get('[title="src/app.ts"]').trigger('click'); await flushPromises()
  expect(preview).toHaveBeenCalledWith({ baseUrl: '/api/agent' }, 'app', 'src/app.ts')
  expect(panel.get('.file-preview-code').text()).toBe('<script>alert(1)</script>')
  expect(panel.find('script, textarea, input, [contenteditable]').exists()).toBe(false)
  expect(panel.text()).toContain('只读')
  panel.unmount()
})

it('refreshes expanded directories and the selected preview after each run and on manual refresh', async () => {
  const list = vi.spyOn(api, 'workspaceFiles').mockImplementation(async (_config, _project, path) => path ? nested : root)
  const preview = vi.spyOn(api, 'previewFile').mockResolvedValue({ path: 'src/app.ts', size: 3, kind: 'text', truncated: false, content: 'old' })
  const panel = mount(WorkspaceFiles, { props: { projectId: 'app' } })
  await flushPromises(); await panel.get('[title="src"]').trigger('click'); await flushPromises()
  await panel.get('[title="src/app.ts"]').trigger('click'); await flushPromises()
  list.mockClear(); preview.mockClear()
  preview.mockResolvedValue({ path: 'src/app.ts', size: 3, kind: 'text', truncated: false, content: 'new' })
  invalidateWorkspaceFiles('app'); await flushPromises()
  expect(list).toHaveBeenCalledTimes(2)
  expect(preview).toHaveBeenCalledOnce()
  expect(panel.get('.file-preview-code').text()).toBe('new')
  expect(panel.get('[title="src"]').attributes('aria-expanded')).toBe('true')
  list.mockClear(); preview.mockClear()
  await panel.get('[title="刷新文件树"]').trigger('click'); await flushPromises()
  expect(list).toHaveBeenCalledTimes(2)
  expect(preview).toHaveBeenCalledOnce()
  panel.unmount()
})

it('paginates directories and ignores a stale preview after switching workspaces', async () => {
  let resolveOld!: (value: Awaited<ReturnType<typeof api.previewFile>>) => void
  const list = vi.spyOn(api, 'workspaceFiles').mockResolvedValueOnce({ ...root, next_offset: 2 })
    .mockResolvedValueOnce({ path: '', entries: [{ name: 'more.txt', path: 'more.txt', kind: 'file' }], next_offset: null })
    .mockResolvedValue({ ...root, entries: [{ name: 'new.txt', path: 'new.txt', kind: 'file' }] })
  vi.spyOn(api, 'previewFile').mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
  const panel = mount(WorkspaceFiles, { props: { projectId: 'app' } })
  await flushPromises(); await panel.get('.tree-more').trigger('click'); await flushPromises()
  expect(list).toHaveBeenLastCalledWith({ baseUrl: '/api/agent' }, 'app', '', 2)
  expect(panel.find('[title="more.txt"]').exists()).toBe(true)
  await panel.get('[title="README.md"]').trigger('click')
  await panel.setProps({ projectId: 'other' }); await flushPromises()
  resolveOld({ path: 'README.md', size: 6, kind: 'text', content: 'STALE', truncated: false }); await flushPromises()
  expect(panel.text()).not.toContain('STALE')
  expect(panel.find('[title="new.txt"]').exists()).toBe(true)
  panel.unmount()
})

it('previews images and makes files available even without a Git repository', async () => {
  vi.spyOn(api, 'workspaceFiles').mockResolvedValue({ path: '', entries: [{ name: 'image.png', path: 'image.png', kind: 'file' }], next_offset: null })
  vi.spyOn(api, 'previewFile').mockResolvedValue({ path: 'image.png', kind: 'image', data_url: 'data:image/png;base64,AAAA', size: 3, truncated: false })
  const panel = mount(SidePanel, { props: { projectId: 'app', git: { available: false } } })
  expect(panel.findAll('.panel-head .tab').map(button => button.text())).toEqual(['变更', '文件'])
  await panel.findAll('button').find(button => button.text() === '文件')!.trigger('click'); await flushPromises()
  expect(panel.findAll('.panel-body > .panel-tabs')).toHaveLength(0)
  await panel.get('[title="image.png"]').trigger('click'); await flushPromises()
  expect(panel.get('.file-preview-image').attributes('src')).toBe('data:image/png;base64,AAAA')
  panel.unmount()
})

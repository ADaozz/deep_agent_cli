import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ThreadScreen from '../components/ThreadScreen.vue'
import { api } from '../api/client'
import { app, loadSessionHistory, loadSessionTranscript, stopStreams } from '../store/app'
import type { SavedTranscript, SavedTranscriptBlock } from '../api/types'
const originalScrollTo = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTo')

function block(id: string, kind: SavedTranscriptBlock['kind'], content: string): SavedTranscriptBlock {
  return { id, kind, content, thinking: '', name: kind === 'tool' ? 'execute' : '',
    arguments: kind === 'tool' ? '{"command":"pwd"}' : '', status: 'completed', is_error: false,
    attachments: [], truncated: false }
}
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: vi.fn() })
  const session = { id: 's', project_id: 'app', created_at: '2026-09-29T00:00:00Z' }
  app.records = { s: { session, title: '旧 CLI 会话', runs: [], draft: '' } }
  app.sessions = [session]
  app.sessionId = 's'
  app.history = {}
  app.transcripts = {}
  vi.spyOn(api, 'projectGit').mockResolvedValue({ available: false })
  vi.spyOn(api, 'sessionRuns').mockResolvedValue({ runs: [], next_cursor: null })
})
afterEach(() => {
  stopStreams(); vi.restoreAllMocks()
  if (originalScrollTo) Object.defineProperty(HTMLElement.prototype, 'scrollTo', originalScrollTo)
  else Reflect.deleteProperty(HTMLElement.prototype, 'scrollTo')
})

it('renders saved CLI history even when there are no HTTP runs, without displaying the empty-session notice', async () => {
  let resolve!: (page: SavedTranscript) => void
  vi.spyOn(api, 'sessionTranscript').mockImplementation(() => new Promise(done => { resolve = done }))
  const create = vi.spyOn(api, 'createRun')
  const request = loadSessionHistory('s')
  const screen = mount(ThreadScreen, { props: { panelOpen: false }, global: { stubs: { PromptComposer: true, WorkLog: true } } })
  await flushPromises()
  expect(screen.text()).not.toContain('此会话还没有运行')
  resolve({ blocks: [block('1', 'user', '请调用 execute 运行 pwd'), block('2', 'tool', '/workspace'), block('3', 'assistant', '**目录已确认**')], next_cursor: null })
  await request; await flushPromises()
  expect(screen.text()).toContain('请调用 execute 运行 pwd')
  expect(screen.text()).toContain('/workspace')
  expect(screen.get('.saved-conversation strong').text()).toBe('目录已确认')
  expect(screen.text()).not.toContain('此会话还没有运行')
  expect(app.records.s!.runs).toHaveLength(0)
  expect(create).not.toHaveBeenCalled()
  screen.unmount()
})

it('loads older saved blocks in order without duplicating the latest page', async () => {
  const transcript = vi.spyOn(api, 'sessionTranscript')
    .mockResolvedValueOnce({ blocks: [block('2', 'assistant', 'newer')], next_cursor: 'checkpoint:1' })
    .mockResolvedValueOnce({ blocks: [block('1', 'user', 'older')], next_cursor: null })
  await loadSessionHistory('s')
  await loadSessionTranscript('s', true)
  expect(transcript).toHaveBeenLastCalledWith({ baseUrl: '/api/agent' }, 's', 'checkpoint:1')
  expect(app.transcripts.s!.blocks.map(item => item.content)).toEqual(['older', 'newer'])
  expect(app.transcripts.s!.cursor).toBeNull()
})

it('shows a history error with retry instead of claiming the session is empty', async () => {
  vi.spyOn(api, 'sessionTranscript').mockRejectedValueOnce(new Error('读取失败'))
    .mockResolvedValueOnce({ blocks: [block('1', 'user', '恢复的旧内容')], next_cursor: null })
  await loadSessionHistory('s')
  const screen = mount(ThreadScreen, { props: { panelOpen: false }, global: { stubs: { PromptComposer: true, WorkLog: true } } })
  expect(screen.text()).toContain('读取失败')
  expect(screen.text()).not.toContain('此会话还没有运行')
  await screen.findAll('button').find(button => button.text() === '重试加载历史')!.trigger('click'); await flushPromises()
  expect(screen.text()).toContain('恢复的旧内容')
  expect(screen.text()).not.toContain('读取失败')
  screen.unmount()
})

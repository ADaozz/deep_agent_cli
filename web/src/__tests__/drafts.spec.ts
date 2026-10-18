import { beforeEach, expect, it } from 'vitest'
import { ref } from 'vue'
import { clearDrafts, clearSentDraft, draftPrompt, readDraft, useDraft, writeDraft } from '../store/drafts'
beforeEach(() => { clearDrafts(); localStorage.clear() })
it('isolates drafts by resume thread ID, persists synchronously and restores browser drafts', () => {
  const key = ref('thread:resume-a')
  const { text, paths } = useDraft(key)
  text.value = 'A draft'
  paths.value = [{ path: '/workspace/有 空格.png', kind: 'image' }]
  expect(JSON.parse(localStorage.getItem('deepagent.draft.v1:thread:resume-a')!)).toEqual(readDraft(key.value))
  key.value = 'thread:resume-b'
  expect(text.value).toBe(''); expect(paths.value).toEqual([])
  text.value = 'B draft'
  key.value = 'thread:resume-a'
  expect(text.value).toBe('A draft'); expect(paths.value[0]?.path).toBe('/workspace/有 空格.png')
  localStorage.setItem('deepagent.draft.v1:thread:restored', JSON.stringify({ text: 'restored', paths: [] }))
  expect(readDraft('thread:restored').text).toBe('restored')
})
it('serializes only text and paths and clears only the unchanged originating draft', () => {
  const a = { text: '', paths: [{ path: '/tmp/a b.png', kind: 'image' as const }, { path: './文件.txt', kind: 'file' as const }] }
  writeDraft('a', a)
  expect(draftPrompt(readDraft('a'))).toBe('/tmp/a b.png\n./文件.txt')
  const snapshot = JSON.stringify(a)
  writeDraft('b', { text: 'keep B', paths: [] })
  writeDraft('a', { ...a, text: 'new edit' })
  clearSentDraft('a', snapshot)
  expect(readDraft('a').text).toBe('new edit')
  clearSentDraft('a', JSON.stringify(readDraft('a')))
  expect(readDraft('a')).toEqual({ text: '', paths: [] })
  expect(readDraft('b').text).toBe('keep B')
  expect(localStorage.getItem('deepagent.draft.v1:a')).toBeNull()
})

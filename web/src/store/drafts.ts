import type { SkillReference } from '../api/types'
import { computed, reactive, type Ref } from 'vue'

export interface PathReference { path: string; kind: 'file' | 'image' }
export interface Draft { text: string; paths: PathReference[]; skills?: SkillReference[] }
const PREFIX = 'deepagent.draft.v1:'
const cache = reactive<Record<string, Draft>>({})

export function readDraft(key: string): Draft {
  if (!cache[key]) {
    let draft: Draft = { text: '', paths: [] }
    try {
      const value = JSON.parse(localStorage.getItem(PREFIX + key) || 'null')
      if (value && typeof value.text === 'string' && Array.isArray(value.paths)) {
        draft = { text: value.text, skills: Array.isArray(value.skills) ? value.skills.filter((s: SkillReference) => s && typeof s.name === 'string' && typeof s.path === 'string' && typeof s.instruction === 'string') : [], paths: value.paths.filter((p: PathReference) => p && typeof p.path === 'string' && (p.kind === 'file' || p.kind === 'image')) }
      }
    } catch { /* A damaged browser draft must not prevent opening the thread. */ }
    cache[key] = draft
  }
  return cache[key]!
}

export function writeDraft(key: string, draft: Draft): void {
  cache[key] = draft
  try {
    if (draft.text || draft.paths.length || draft.skills?.length) localStorage.setItem(PREFIX + key, JSON.stringify(draft))
    else localStorage.removeItem(PREFIX + key)
  } catch { /* Keep the draft usable when browser storage is unavailable. */ }
}

export function useDraft(key: Ref<string>) {
  const text = computed({ get: () => readDraft(key.value).text, set: value => writeDraft(key.value, { ...readDraft(key.value), text: value }) })
  const paths = computed({ get: () => readDraft(key.value).paths, set: value => writeDraft(key.value, { ...readDraft(key.value), paths: value }) })
  const skills = computed({ get: () => readDraft(key.value).skills || [], set: value => writeDraft(key.value, { ...readDraft(key.value), skills: value }) })
  return { text, paths, skills }
}

/** Paths are plain prompt text; never read or upload file contents. */
export function draftPrompt(draft: Draft): string {
  return [draft.text.trim(), ...draft.paths.map(p => p.path), ...(draft.skills || []).map(s => s.instruction)].filter(Boolean).join('\n')
}

export function clearSentDraft(key: string, snapshot: string): void {
  // A send can complete after the user switches thread or edits the draft.
  if (JSON.stringify(readDraft(key)) === snapshot) writeDraft(key, { text: '', paths: [] })
}

export function clearDrafts(): void {
  for (const key of Object.keys(cache)) delete cache[key]
  try {
    for (const key of Object.keys(localStorage)) if (key.startsWith(PREFIX)) localStorage.removeItem(key)
  } catch { /* ignore unavailable storage */ }
}

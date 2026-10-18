import { reactive } from 'vue'
import { api } from '../api/client'
import { apiConfig } from './settings'
import { models } from './models'
export const permissionDrafts = reactive<Record<string, { mode: 'ask' | 'allow'; confirmation: string }>>({})
export async function applyDraftPermission(projectId: string, sessionId: string): Promise<void> {
  const draft = permissionDrafts[projectId]
  if (draft) {
    models.changing[sessionId] = true
    try { await api.selectPermission(apiConfig(), sessionId, draft.mode, draft.confirmation) }
    finally { models.changing[sessionId] = false }
  }
}

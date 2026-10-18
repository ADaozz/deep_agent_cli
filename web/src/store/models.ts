import { reactive } from 'vue'
import { api } from '../api/client'
import type { ModelCatalog, ModelSelection } from '../api/types'
import { apiConfig } from './settings'

export const models = reactive({
  changing: {} as Record<string, boolean>,
  catalogs: {} as Record<string, ModelCatalog>,
  drafts: {} as Record<string, ModelSelection>,
  sessions: {} as Record<string, ModelSelection>,
})

const versions = new Map<string, number>()

export async function loadModels(projectId: string, sessionId = ''): Promise<void> {
  const key = `${projectId}:${sessionId}`
  const version = (versions.get(key) || 0) + 1
  versions.set(key, version)
  const [catalog, current] = await Promise.all([
    api.models(apiConfig(), projectId),
    sessionId ? api.sessionModel(apiConfig(), sessionId) : Promise.resolve(null),
  ])
  if (versions.get(key) !== version) return
  models.catalogs[projectId] = catalog
  if (current && sessionId) models.sessions[sessionId] = current
}

export async function selectModel(projectId: string, sessionId: string, modelId: string, effort: string): Promise<void> {
  if (sessionId) {
    const key = `${projectId}:${sessionId}`
    versions.set(key, (versions.get(key) || 0) + 1)
    models.changing[sessionId] = true
    try { models.sessions[sessionId] = await api.selectModel(apiConfig(), sessionId, modelId, effort) }
    finally { models.changing[sessionId] = false }
  } else {
    const profile = models.catalogs[projectId]?.models.find(item => item.id === modelId)
    if (!profile || !profile.reasoning_efforts.includes(effort)) throw new Error('模型配置已变化，请重新选择')
    models.drafts[projectId] = { model_id: profile.id, model: profile.model, source: profile.source, reasoning_effort: effort }
  }
}

/** Apply the home pick to the newly created session before starting its first run. */
export async function applyDraftModel(projectId: string, sessionId: string): Promise<void> {
  const selection = models.drafts[projectId]
  if (selection) await selectModel(projectId, sessionId, selection.model_id, selection.reasoning_effort)
}

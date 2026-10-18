import { reactive } from 'vue'
import { api } from '../api/client'
import type { WorkspaceGit } from '../api/types'
import { apiConfig } from './settings'

export const workspaceGit = reactive<Record<string, WorkspaceGit>>({})
const pending = new Map<string, Promise<void>>()
export function refreshGit(projectId: string): Promise<void> {
  if (!projectId) return Promise.resolve()
  const existing = pending.get(projectId)
  if (existing) return existing
  const request = api.projectGit(apiConfig(), projectId)
    .then(result => { workspaceGit[projectId] = result })
    .catch(() => { workspaceGit[projectId] = { available: false } })
    .finally(() => pending.delete(projectId))
  pending.set(projectId, request)
  return request
}

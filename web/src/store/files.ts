import { reactive } from 'vue'

export const workspaceFileVersions = reactive<Record<string, number>>({})

export function invalidateWorkspaceFiles(projectId: string): void {
  if (projectId) workspaceFileVersions[projectId] = (workspaceFileVersions[projectId] || 0) + 1
}

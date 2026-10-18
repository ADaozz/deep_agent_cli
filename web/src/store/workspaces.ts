import { reactive } from 'vue'
const KEY = 'deepagent.workspace-history.v1'
function read(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(KEY) || '[]')
    return Array.isArray(value) ? [...new Set(value.filter((path): path is string => typeof path === 'string'))].slice(0, 4) : []
  } catch { return [] }
}
export const recentWorkspacePaths = reactive<string[]>(read())
export function rememberWorkspace(path: string): void {
  recentWorkspacePaths.splice(0, recentWorkspacePaths.length, path, ...recentWorkspacePaths.filter(item => item !== path).slice(0, 3))
  try { localStorage.setItem(KEY, JSON.stringify(recentWorkspacePaths)) } catch { /* Selection still works in memory. */ }
}

import { reactive } from 'vue'
// Recent selections are page memory, never browser-persisted preferences.
export const recentWorkspacePaths = reactive<string[]>([])
export function rememberWorkspace(path: string): void {
  recentWorkspacePaths.splice(0, recentWorkspacePaths.length, path, ...recentWorkspacePaths.filter(item => item !== path).slice(0, 3))
}

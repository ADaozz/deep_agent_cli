import './browserStorage'
import { reactive } from 'vue'

export const DEFAULT_BASE_URL = '/api/agent'
export const PROXY_BASE_URL = DEFAULT_BASE_URL

export interface Settings {
  baseUrl: string
  projectLabels: Record<string, string>
}

// Display preferences live only for the current page.
export const settings = reactive<Settings>({ baseUrl: DEFAULT_BASE_URL, projectLabels: {} })
export function applySettings(next: Settings): void {
  Object.assign(settings, next, { baseUrl: DEFAULT_BASE_URL })
}
export function apiConfig(): { baseUrl: string } { return { baseUrl: DEFAULT_BASE_URL } }
export function projectLabel(projectId: string): string { return settings.projectLabels[projectId]?.trim() || projectId }

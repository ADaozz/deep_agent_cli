import { reactive } from 'vue'

const STORAGE_KEY = 'deepagent.settings.v1'
export const DEFAULT_BASE_URL = '/api/agent'
export const PROXY_BASE_URL = DEFAULT_BASE_URL
export const DEFAULT_MODEL_LABEL = 'Deep Agent'

export interface Settings {
  baseUrl: string
  projectLabels: Record<string, string>
  modelLabel: string
}

function read(): Settings {
  const fallback = { baseUrl: DEFAULT_BASE_URL, projectLabels: {}, modelLabel: DEFAULT_MODEL_LABEL }
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    return { ...fallback, projectLabels: parsed.projectLabels || {}, modelLabel: parsed.modelLabel || DEFAULT_MODEL_LABEL }
  } catch { return fallback }
}

export const settings = reactive<Settings>(read())
export function persistSettings(): void {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)) } catch { /* Memory state remains usable. */ }
}
export function applySettings(next: Settings): void {
  Object.assign(settings, next, { baseUrl: DEFAULT_BASE_URL })
  persistSettings()
}
export function apiConfig(): { baseUrl: string } { return { baseUrl: DEFAULT_BASE_URL } }
export function projectLabel(projectId: string): string { return settings.projectLabels[projectId]?.trim() || projectId }
// Rewrite legacy settings without browser tokens or remote service addresses.
persistSettings()

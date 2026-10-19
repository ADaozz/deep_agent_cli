import { ref } from 'vue'

export type Appearance = 'light' | 'dark'
const STORAGE_KEY = 'deepagent.appearance.v1'
let saved: Appearance = 'light'
try { if (localStorage.getItem(STORAGE_KEY) === 'dark') saved = 'dark' } catch { /* Use the default when storage is unavailable. */ }
export const appearance = ref<Appearance>(saved)

function apply(): void { document.documentElement.dataset.theme = appearance.value }
apply()

export function setAppearance(value: Appearance): void {
  appearance.value = value
  apply()
  try { localStorage.setItem(STORAGE_KEY, value) } catch { /* The choice still works for this page. */ }
}

import { ref } from 'vue'

export const homePath = ref('')

export function displayPath(path?: string): string {
  if (!path) return ''
  const home = homePath.value.replace(/\/+$/, '')
  if (!home) return path
  if (path === home) return '~'
  return path.startsWith(home + '/') ? '~' + path.slice(home.length) : path
}

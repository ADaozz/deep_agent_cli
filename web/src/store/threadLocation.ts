/** Thread navigation belongs in the URL, never in localStorage. */
export function readThreadLocation(): string {
  return new URLSearchParams(window.location.hash.slice(1)).get('thread') || ''
}
export function writeThreadLocation(id: string): void {
  const url = new URL(window.location.href)
  url.hash = id ? new URLSearchParams({ thread: id }).toString() : ''
  window.history.replaceState(null, '', url)
}

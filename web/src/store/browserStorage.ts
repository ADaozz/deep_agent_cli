/** Retire old caches/preferences; localStorage is reserved for composer drafts. */
const LEGACY_KEYS = [
  'deepagent.records.v1',
  'deepagent.last-session.v1',
  'deepagent.settings.v1',
  'deepagent.workspace-history.v1',
]
try {
  for (const key of LEGACY_KEYS) localStorage.removeItem(key)
} catch { /* Storage can be unavailable; the page still works in memory. */ }

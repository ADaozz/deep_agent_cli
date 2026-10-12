// Tool and field IDs are opaque server strings, never Object prototype members.
export function dictionary<T>(): Record<string, T> { return Object.create(null) as Record<string, T>; }

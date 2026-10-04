// The console's API client: the session cookie rides along, every POST carries X-Shortbot (the CSRF guard).

export interface RunResult<T = unknown> { result: T; ms: number; stale_code?: string }
export type Settled<T> = { ok: true; res: RunResult<T> } | { ok: false; err: string }

let onUnauthorized: () => void = () => {}
export const setUnauthorized = (fn: () => void) => { onUnauthorized = fn }

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { credentials: 'same-origin', headers: { 'X-Shortbot': '1' } }
  if (body !== undefined) {
    init.method = 'POST'
    init.headers = { 'X-Shortbot': '1', 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }
  const res = await fetch(path, init)
  if (res.status === 401 && path !== '/api/login') {
    onUnauthorized()
    throw new Error('log in first')
  }
  const type = res.headers.get('content-type') ?? ''
  const data = type.includes('json') ? await res.json() : await res.text()
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`)
  return data as T
}

// A desk command, as the MCP tool runs it (web/runner.py): reads return at once, writes go through the gateway.
export const run = <T = unknown>(key: string, args: Record<string, unknown> = {}) =>
  api<RunResult<T>>(`/api/run/${encodeURIComponent(key)}`, { args })

export async function settle<T>(key: string, args?: Record<string, unknown>): Promise<Settled<T>> {
  try {
    return { ok: true, res: await run<T>(key, args) }
  } catch (err) {
    return { ok: false, err: (err as Error).message }
  }
}

export const store = {
  get(k: string, d: string): string {
    try { return localStorage.getItem(k) ?? d } catch { return d }   // storage may be blocked
  },
  set(k: string, v: string) {
    try { localStorage.setItem(k, v) } catch { /* storage blocked */ }
  },
}

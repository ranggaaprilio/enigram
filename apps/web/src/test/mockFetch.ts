import { vi } from 'vitest'

export interface Reply {
  status?: number
  body: unknown
}

// Routes are keyed "METHOD /api/path"; unknown routes answer 404 like the API would.
export function mockFetch(routes: Record<string, Reply>) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const reply = routes[`${init?.method ?? 'GET'} ${url}`] ?? {
      status: 404,
      body: { detail: `No route for ${url}` },
    }
    return new Response(JSON.stringify(reply.body), {
      status: reply.status ?? 200,
      headers: { 'content-type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

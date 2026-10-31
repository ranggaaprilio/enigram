// Typed client for the FastAPI service. Every path goes through /api: the Vite proxy in dev,
// nginx in the container.

export type DocStatus =
  | 'queued'
  | 'running'
  | 'parsed'
  | 'indexed'
  | 'failed'
  | 'done'
  | 'deleting'

export interface Job {
  id: number
  kind: string
  payload: Record<string, unknown>
  status: DocStatus
  result: Record<string, unknown> | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface DocumentRow {
  book: string
  file: string | null
  title: string | null
  status: DocStatus | null
  pages: number | null
  chunks: number | null
  error: string | null
  updated_at: string | null
  repo: string | null // the Git repository it was indexed from
  job: Job | null
}

export interface UploadResult {
  book: string
  status: DocStatus
  job: Job | null
}

export interface Source {
  ref: number
  title: string
  section: string // "-" when the chunk had no heading, "" when the citation was truncated
  pages: string
  citation: string
}

export interface ContextChunk {
  ref: number
  chunk_id: string
  text: string
}

export interface ContextEntity {
  name: string
  type: string
  description: string
}

export interface ContextRelation {
  source: string
  target: string
  keywords: string
  description: string
}

export type Mode = 'mix' | 'naive'

export interface QueryResult {
  question: string
  answer: string
  sources: Source[]
  chunks: ContextChunk[]
  entities: ContextEntity[]
  relations: ContextRelation[]
  found: boolean
  query_secs: number
  mode: Mode
}

export interface QuerySummary {
  id: number
  source: string
  question: string
  mode: Mode
  found: boolean | null
  query_secs: number | null
  error: string | null
  created_at: string
}

export interface QueryRecord extends QuerySummary {
  result: QueryResult | null
}

export interface GraphEntity {
  name: string
  type: string | null
  description: string | null
}

export interface GraphRelation {
  source: string
  target: string
  keywords: string | null
  description: string | null
}

export interface GraphSearch {
  entities: GraphEntity[]
  relations: GraphRelation[]
}

// Graph edits run in the index worker: the API answers with the queued job.
export interface JobAccepted {
  job: Job
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// FastAPI: {"detail": "text"} for HTTPException, {"detail": [{"msg": ...}]} for validation.
// nginx error pages are HTML, so fall back to the status line.
function errorMessage(res: Response, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && typeof detail[0]?.msg === 'string') return detail[0].msg
  return `${res.status} ${res.statusText}`.trim()
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, init)
  const body: unknown = await res.json().catch(() => null)
  if (!res.ok) throw new ApiError(res.status, errorMessage(res, body))
  return body as T
}

export const getHealth = () => request<{ status: string; workspace: string }>('/health')

export const listDocuments = () => request<DocumentRow[]>('/documents')

export function uploadDocument(file: File) {
  const form = new FormData()
  form.append('file', file)
  return request<UploadResult>('/documents', { method: 'POST', body: form })
}

export const getJob = (id: number) => request<Job>(`/jobs/${id}`)

// A worker job: the document stays listed as `deleting` until it is gone.
export const deleteDocument = (book: string) =>
  request<UploadResult>(`/documents/${encodeURIComponent(book)}`, { method: 'DELETE' })

const sendJson = <T>(path: string, body: unknown) =>
  request<T>(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })

export const ask = (question: string, mode: Mode) =>
  sendJson<QueryResult & { id: number }>('/query', { question, mode })

export const listQueries = (limit = 50) => request<QuerySummary[]>(`/queries?limit=${limit}`)

export const getQuery = (id: number) => request<QueryRecord>(`/queries/${id}`)

export const searchGraph = (q: string, limit = 50) =>
  request<GraphSearch>(`/graph/search?q=${encodeURIComponent(q)}&limit=${limit}`)

export const editEntity = (body: { name: string; description?: string; entity_type?: string }) =>
  sendJson<JobAccepted>('/graph/entity/edit', body)

export const editRelation = (body: {
  source: string
  target: string
  description?: string
  keywords?: string
}) => sendJson<JobAccepted>('/graph/relation/edit', body)

export const mergeEntities = (body: { sources: string[]; target: string; description?: string }) =>
  sendJson<JobAccepted>('/graph/entity/merge', body)

export const deleteEntity = (name: string) =>
  request<JobAccepted>(`/graph/entity/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const deleteRelation = (source: string, target: string) =>
  request<JobAccepted>(
    `/graph/relation?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}`,
    { method: 'DELETE' },
  )

export const indexRepo = (url: string, ref?: string) =>
  sendJson<{ repo: string; job: Job }>('/repos', ref ? { url, ref } : { url })

export function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

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
  | 'budget_exceeded' // a worker job stopped by MAX_TOKENS_PER_JOB or DAILY_TOKEN_BUDGET

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
  origin?: string // code | summary | human | document; absent on answers before Phase 2
  target?: string // "<repo id>:<symbol id>" for a code source, the note id for a human one
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
  mode: Mode | 'enigram-agent'
}

export type Scope = 'docs' | 'code'

export type AnnotationStatus =
  | 'verified'
  | 'needs_verification'
  | 'orphaned'
  | 'proposed'
  | 'rejected'

export type TargetKind = 'symbol' | 'entity' | 'document'

export interface Candidate {
  id: string
  qualified_name: string
  file: string
}

export interface Annotation {
  id: number
  target_kind: TargetKind
  target_ref: string
  repo_id: number | null
  scope: 'deps' | 'body'
  text: string
  author: string
  by: string
  owner: string
  status: AnnotationStatus
  reason: string | null
  conflict: boolean
  target_hash: string | null
  deps_hash: string | null
  candidates: Candidate[]
  verified_by: string | null
  verified_at: string | null
  verify_by: string | null
  created_at: string
  updated_at: string
}

export interface AnnotationEvent {
  id: number
  annotation_id: number
  actor: string
  action: string
  old: Record<string, unknown> | null
  new: Record<string, unknown> | null
  at: string
}

export interface AnnotationWithEvents extends Annotation {
  events: AnnotationEvent[]
}

export interface NewAnnotation {
  target_kind: TargetKind
  target_ref: string
  text: string
  repo?: number
  by?: string
  scope?: 'deps' | 'body'
  verify_by?: string
}

export interface AnnotationPatch {
  status?: 'verified' | 'rejected' | 'needs_verification'
  text?: string
  owner?: string
  verify_by?: string
  conflict?: boolean
  target_ref?: string
  by?: string
}

export interface RepoRow {
  id: number
  url: string
  ref: string | null
  name: string
  indexed_sha: string
  indexed_at: string | null
  webhook: boolean
}

export interface SymbolRow {
  id: string
  qualified_name: string
  kind: string
  file: string
  line_start: number
  line_end: number
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

export const ask = (question: string, mode: Mode, scope: Scope = 'docs') =>
  sendJson<QueryResult & { id: number }>('/query', { question, mode, scope })

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

export function listAnnotations(params: {
  status?: string
  target_kind?: string
  repo?: number
  target_ref?: string
}) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') query.set(key, String(value))
  }
  const qs = query.toString()
  return request<Annotation[]>(`/annotations${qs ? `?${qs}` : ''}`)
}

export const createAnnotation = (body: NewAnnotation) => sendJson<Annotation>('/annotations', body)

export const getAnnotation = (id: number) => request<AnnotationWithEvents>(`/annotations/${id}`)

export const patchAnnotation = (id: number, body: AnnotationPatch) =>
  request<Annotation>(`/annotations/${id}`, {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })

export const listRepos = () => request<RepoRow[]>('/repos')

export const searchSymbols = (repoId: number, q: string) =>
  request<SymbolRow[]>(`/repos/${repoId}/symbols?q=${encodeURIComponent(q)}`)

export const deleteRepo = (id: number) =>
  request<{ repo: number; job: Job }>(`/repos/${id}`, { method: 'DELETE' })

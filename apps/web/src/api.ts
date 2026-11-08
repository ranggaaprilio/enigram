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
  // 'auto': the router merged a code answer and a documents answer.
  mode: Mode | 'enigram-agent' | 'auto'
  extra?: { route?: RouteDecision }
}

// The router's decision (application/routing.py), on answers from scope 'auto' or forced.
export interface RouteDecision {
  route: string
  targets: string[]
  method: string
  rules: string[]
  confidence: number | null
}

export type Scope = 'auto' | 'docs' | 'code'

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
  via: 'api' | 'mcp' // mcp: an agent's proposal; only another verifier key may approve it
  question: string | null // the question an MCP proposal answers
  memory_id: number | null // the agent memory it was promoted from
  note_type: NoteType
  // The summary the Code page showed when the note was written or last verified (symbol notes
  // only; null on older notes): kept for the re-check, as a re-index deletes old summaries.
  // The GET /annotations list leaves the text out.
  summary_kind: 'flow' | 'symbol' | null
  summary_text?: string | null
}

// What a note claims; old notes and agent proposals are technical.
export type NoteType = 'business_rule' | 'expected_behavior' | 'known_issue' | 'technical'

export const NOTE_TYPES: Record<NoteType, string> = {
  business_rule: 'Business rule',
  expected_behavior: 'Expected behavior',
  known_issue: 'Known issue',
  technical: 'Technical',
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
  owner?: string
  verify_by?: string
  note_type?: NoteType
  proposed?: boolean // stay a proposal even for the owner key (a Code-page claim about the code)
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

// One entry point of a repository (code_browse.features_view); a symbol with two routes is two rows.
export interface Feature {
  symbol_id: string
  qualified_name: string
  kind: string // http_route, cron, ...
  route: string
  framework: string
  file: string
  line_start: number
  line_end: number
  notes: number // active notes on the symbol
  reviewed: boolean // a current "looks right" marker on its summary
}

export interface FeatureList {
  features: Feature[] // at most the first 500 (FEATURE_LIST_MAX)
  total: number
}

export interface SymbolStep {
  id: string
  qualified_name: string
  kind: string
  file: string
  line_start: number
  line_end: number
  summary: string | null
  notes: number
}

// GET /repos/{id}/symbols/{sid} (code_browse.symbol_view)
export interface SymbolView {
  symbol: {
    id: string
    qualified_name: string
    kind: string
    file: string
    line_start: number
    line_end: number
    parent: string | null
    signature: string | null
    exported: boolean
    decorators: string[]
    body: string
  }
  location: string // "src/a.ts:8-11@<sha12>"
  entrypoints: { kind: string; route: string; framework: string }[]
  summary: { flow: string | null; symbol: string | null }
  steps: SymbolStep[]
  external: string[]
  callers: SymbolRef[] // repository symbols that call this one directly
  notes: Annotation[]
  review: Review | null
}

export interface SymbolRef {
  id: string
  qualified_name: string
  kind: string
  file: string
  line_start: number
  line_end: number
  notes: number
}

// GET /repos/{id}/tree (code_browse.tree_view): every indexed file, its symbols in line order.
export interface TreeSymbol {
  id: string
  qualified_name: string
  kind: string
  line_start: number
  line_end: number
  parent: string | null
  notes: number
}

export interface TreeFile {
  path: string
  symbols: TreeSymbol[]
}

// A "looks right" marker on a symbol's summary; `current` is false once the summary changed.
export interface Review {
  by: string
  actor: string
  at: string
  current: boolean
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

export interface Workspace {
  name: string
  created_at: string
}

// The workspace every request works on (X-Enigram-Workspace), set by the page shell from the
// URL; null sends none, which the API answers from its process workspace.
let workspace: string | null = null

export function setWorkspace(name: string | null) {
  workspace = name
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, withHeaders(init, workspace))
  const body: unknown = await res.json().catch(() => null)
  if (!res.ok) throw new ApiError(res.status, errorMessage(res, body))
  return body as T
}

function withHeaders(init: RequestInit | undefined, name: string | null): RequestInit {
  const headers = new Headers(init?.headers)
  if (name) headers.set('X-Enigram-Workspace', name)
  // A signed-in user's writes ride on the session cookie; the API wants this header on them,
  // which a cross-site form cannot send (R4).
  headers.set('X-Requested-With', 'enigram')
  return { ...init, headers }
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

export const listWorkspaces = () => request<Workspace[]>('/workspaces')

// GET /me: the caller and its role per workspace; principal null when the API has no keys.
export interface Me {
  principal: {
    name: string
    kind: string // key | user (signed in with the login provider)
    owner: boolean
    default_workspace: string | null
    display_name?: string | null
  } | null
  roles: Record<string, string>
}

export const getMe = () => request<Me>('/me')

// Whether the API can sign people in (OIDC is set); login itself is a page redirect.
export const getAuthConfig = () => request<{ login: boolean }>('/auth/config')

export const logout = () => request<null>('/auth/logout', { method: 'POST' })

export const loginUrl = (next: string) => `/api/auth/login?next=${encodeURIComponent(next)}`

// Members and keys of the current workspace (docs/rbac.md): its admins only.
export type Role = 'viewer' | 'contributor' | 'maintainer' | 'admin'

export interface Member {
  name: string
  kind: string
  role: Role
  owner: boolean
}

export const listMembers = () => request<Member[]>('/members')

export const setMember = (name: string, role: Role) =>
  request<{ name: string; role: Role }>(`/members/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ role }),
  })

export const removeMember = (name: string) =>
  request<null>(`/members/${encodeURIComponent(name)}`, { method: 'DELETE' })

// The key is in this answer only: the API keeps its hash.
export const createKey = (name: string, role: Role) =>
  sendJson<{ name: string; role: Role; key: string }>('/keys', { name, role })

export const revokeKey = (name: string) =>
  request<null>(`/keys/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const createWorkspace = (name: string) => sendJson<Workspace>('/workspaces', { name })

export const ask = (question: string, mode: Mode, scope: Scope = 'auto') =>
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

// `token`: a private repository's access token; the API stores it encrypted, never in the job.
export const indexRepo = (url: string, ref?: string, token?: string) =>
  sendJson<{ repo: string; job: Job }>('/repos', {
    url,
    ...(ref ? { ref } : {}),
    ...(token ? { token } : {}),
  })

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

// GET /review: notes waiting for a person (plan item 5, A2), with their context.
export type ReviewReason = 'proposed' | 'needs_verification' | 'orphaned' | 'conflict'

export interface ReviewItem {
  reason: ReviewReason
  note: Annotation
  repo_name: string | null
  summary_then: string | null // the summary the note was written against
  summary_now: string | null
  summary_now_kind: 'flow' | 'symbol' | null
}

export const getReview = () => request<ReviewItem[]>('/review')

// GET /targets: what a note can be about, found by what people know (plan item 5, A1).
export interface Target {
  kind: 'feature' | 'symbol' | 'entity' | 'document' // how the form groups it
  target_kind: TargetKind
  target_ref: string
  repo: number | null
  repo_name: string | null
  label: string
  detail: string
  summary: string | null
}

export const searchTargets = (q: string) => request<Target[]>(`/targets?q=${encodeURIComponent(q)}`)

export const getAnnotation = (id: number) => request<AnnotationWithEvents>(`/annotations/${id}`)

export const patchAnnotation = (id: number, body: AnnotationPatch) =>
  request<Annotation>(`/annotations/${id}`, {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })

export const deleteAnnotation = (id: number) =>
  request<null>(`/annotations/${id}`, { method: 'DELETE' })

export const listRepos = () => request<RepoRow[]>('/repos')

export const listFeatures = (repoId: number, q = '') =>
  request<FeatureList>(`/repos/${repoId}/features${q ? `?q=${encodeURIComponent(q)}` : ''}`)

export const getTree = (repoId: number) => request<{ files: TreeFile[] }>(`/repos/${repoId}/tree`)

// Symbol ids are "<file>#<qualified_name>": encode them as one path segment (%2F, %23).
export const getSymbol = (repoId: number, symbolId: string) =>
  request<SymbolView>(`/repos/${repoId}/symbols/${encodeURIComponent(symbolId)}`)

export const reviewSymbol = (repoId: number, symbolId: string, by: string) =>
  sendJson<Review>(`/repos/${repoId}/symbols/${encodeURIComponent(symbolId)}/review`, { by })

export const unreviewSymbol = (repoId: number, symbolId: string) =>
  request<null>(`/repos/${repoId}/symbols/${encodeURIComponent(symbolId)}/review`, {
    method: 'DELETE',
  })

export const deleteRepo = (id: number) =>
  request<{ repo: number; job: Job }>(`/repos/${id}`, { method: 'DELETE' })

// GET /stats: the Dashboard's numbers (plan item 6, D1). Days are UTC days ("2026-11-08"); the
// daily series hold only days with data.
export interface Stats {
  workspace: string
  days: number
  since: string
  tokens: {
    budget: number | null // null: unlimited
    per_key_budget: number | null
    today: number
    left_today: number | null
    by_day: { day: string; source: string; tokens: number }[]
    by_key: { day: string; api_key: string; tokens: number }[] | null // admin only
  }
  questions: {
    asked: number
    unanswered: number
    errors: number
    p50_secs: number | null
    p95_secs: number | null
    by_day: { day: string; asked: number; unanswered: number; errors: number }[]
  }
  answers: {
    routes: { targets: string; method: string; count: number }[]
    origins: Partial<Record<'code' | 'summary' | 'human' | 'document' | 'unsupported', number>>
  }
  knowledge: {
    documents: Record<string, number>
    repos: {
      id: number
      name: string
      url: string
      ref: string | null
      indexed_sha: string
      indexed_at: string
      files: number
      symbols: number
      entrypoints: number
    }[]
  }
  review: {
    entrypoints: { entrypoints: number; reviewed: number; current: number }
    notes: { note_type: NoteType; status: string; count: number }[]
  }
  attention: {
    proposed: number
    needs_verification: number
    orphaned: number
    note_conflicts: number
    doc_conflicts: number
    failed_jobs: number
    budget_exceeded_jobs: number
    jobs_since_days: number
  }
}

export const getStats = (days: number) => request<Stats>(`/stats?days=${days}`)

// GET /stats/repos and /stats/repos/{id} (plan item 6, D2b).
export type RemoteStatus = 'current' | 'behind' | 'unreachable' | 'unchecked'

export interface IndexRun {
  job_id: number
  status: string // queued | running | done | failed | budget_exceeded
  trigger: string | null // manual | poll | webhook
  created_at: string
  finished_at: string | null
  secs: number | null
  commit_sha: string | null
  commit_at: string | null
  tokens: number | null
  files_changed: number | null
  symbols_changed: number | null
  staleness: Record<string, number> | null
  error: string | null
}

export interface RepoStat {
  id: number
  name: string
  url: string
  ref: string | null
  indexed_sha: string
  commit_at: string | null // the indexed commit's date; null when no job recorded it
  indexed_at: string
  remote: {
    status: RemoteStatus
    sha: string | null
    checked_at: string | null
    error: string | null
  }
  updates: 'webhook' | 'poll' | 'manual'
  last_run: IndexRun | null
  files: number
  symbols: number
  entrypoints: number
  documents: number
}

export interface RepoStatDetail extends RepoStat {
  runs: IndexRun[]
  quality: {
    adapters: Record<string, number> | null
    fallback: string | null
    unmatched_frameworks: string[] | null
    files_skipped: Record<string, number> | null
    calls_resolved_share: number | null
    calls_in_repo_share: number | null
    diagnostics_sample: string[] | null
    secrets_redacted: number | null
  }
  summaries: Record<string, number> | null
  staleness: Record<string, number> | null
}

export const getRepoStats = () => request<RepoStat[]>('/stats/repos')
export const getRepoStat = (id: number) => request<RepoStatDetail>(`/stats/repos/${id}`)

// GET /stats/workspaces: every workspace against the daily budget, for an owner (plan item 6, D3).
export interface WorkspaceStatRow {
  workspace: string
  tokens_today: number
  left_today: number | null
  tokens_period: number
  asked: number
  unanswered: number
  waiting: number
  failed_jobs: number
}

export interface WorkspaceStats {
  days: number
  since: string
  budget: number | null // each workspace's daily cap; null: unlimited
  jobs_since_days: number
  workspaces: WorkspaceStatRow[]
  total: Omit<WorkspaceStatRow, 'workspace' | 'left_today'>
}

export const getWorkspaceStats = (days: number) =>
  request<WorkspaceStats>(`/stats/workspaces?days=${days}`)

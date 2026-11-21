import type { DocStatus, DocumentRow, Job, JobStage, RepoStat } from '../../api'

// What the Pipeline page lists: Git repositories (with the documents indexed from them) and
// uploaded files.
export interface RepoSource {
  kind: 'repo'
  key: string
  url: string
  name: string
  stat: RepoStat | null // null until the code graph knows it (a first sync, a Markdown-only repo)
  job: Job | null // its queued or running sync
  finished: Job | null // a sync this page saw end
  docs: DocumentRow[]
}

export interface FileSource {
  kind: 'file'
  key: string
  doc: DocumentRow
}

export type Source = RepoSource | FileSource

export const ACTIVE_DOC: (DocStatus | null)[] = ['queued', 'running', 'parsed', 'deleting']

// "https://github.com/org/handbook" → "github.com/org/handbook"
export const repoLabel = (url: string) => url.replace(/^https:\/\//, '')

const urlOf = (job: Job) => (typeof job.payload.url === 'string' ? job.payload.url : null)

export function repoSummary(result: Record<string, unknown> | null): string {
  if (!result) return ''
  const count = (key: string) => (Array.isArray(result[key]) ? result[key].length : 0)
  return `${String(result.files)} files, ${count('indexed')} indexed, ${String(result.unchanged)} unchanged, ${count('deleted')} deleted`
}

export function buildSources(
  docs: DocumentRow[],
  repos: RepoStat[],
  jobs: Job[],
  finished: Record<string, Job>,
): Source[] {
  const byUrl = new Map<string, RepoSource>()
  const repo = (url: string): RepoSource => {
    let source = byUrl.get(url)
    if (!source) {
      source = {
        kind: 'repo',
        key: `repo:${url}`,
        url,
        name: repoLabel(url),
        stat: null,
        job: null,
        finished: null,
        docs: [],
      }
      byUrl.set(url, source)
    }
    return source
  }
  for (const stat of repos) Object.assign(repo(stat.url), { stat, name: stat.name })
  for (const job of jobs) {
    const url = job.kind === 'index_repo' ? urlOf(job) : null
    if (url) repo(url).job = job
  }
  for (const [url, job] of Object.entries(finished)) repo(url).finished = job
  const files: FileSource[] = []
  for (const doc of docs) {
    if (doc.repo) repo(doc.repo).docs.push(doc)
    else files.push({ kind: 'file', key: `file:${doc.book}`, doc })
  }
  return [...byUrl.values(), ...files].sort(
    (a, b) => rank(a) - rank(b) || nameOf(a).localeCompare(nameOf(b)),
  )
}

export const nameOf = (s: Source) => (s.kind === 'repo' ? s.name : (s.doc.title ?? s.doc.book))

export const isActive = (s: Source) =>
  s.kind === 'repo' ? s.job !== null : ACTIVE_DOC.includes(s.doc.status)

export function isFailed(s: Source): boolean {
  if (s.kind === 'file') return s.doc.status === 'failed' || s.doc.status === 'budget_exceeded'
  if (s.job) return false
  return s.finished?.status === 'failed' || s.finished?.status === 'budget_exceeded'
}

// Working first, then what failed, then the rest by name.
const rank = (s: Source) => (isActive(s) ? 0 : isFailed(s) ? 1 : 2)

// The steps a sync shows: "queued", then the job's own stages. Until the scan has run only
// clone and scan are known, so a last "index" step stands for whatever comes next.
export type Step = 'queued' | JobStage | 'index'

export const STEP_LABEL: Record<Step, string> = {
  queued: 'Queued',
  clone: 'Clone',
  scan: 'Scan',
  code: 'Code graph',
  docs: 'Documents',
  index: 'Index',
}

export function stepsOf(job: Job): { steps: Step[]; current: number } {
  const stages = job.progress?.detail.stages ?? ['clone', 'scan']
  const steps: Step[] = ['queued', ...stages, ...(stages.length <= 2 ? ['index' as const] : [])]
  if (job.status === 'queued') return { steps, current: 0 }
  // A worker from before stages were recorded: running, stage unknown.
  const current = steps.indexOf(job.progress?.stage ?? 'clone')
  return { steps, current: Math.max(current, 1) }
}

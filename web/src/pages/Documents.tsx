import {
  useCallback,
  useEffect,
  useState,
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
} from 'react'
import {
  ApiError,
  deleteDocument,
  getJob,
  indexRepo,
  listDocuments,
  messageOf,
  uploadDocument,
  type DocStatus,
  type DocumentRow,
  type Job,
} from '../api'
import { formatTime } from '../format'

const POLL_MS = 3000
const ACTIVE: (DocStatus | null)[] = ['queued', 'running', 'parsed', 'deleting']

interface RepoJob {
  jobId: number
  url: string
  status: Job['status']
  error: string | null
  result: Record<string, unknown> | null
}

const repoActive = (r: RepoJob) => r.status === 'queued' || r.status === 'running'

function repoSummary(result: Record<string, unknown> | null): string {
  if (!result) return ''
  const count = (key: string) => (Array.isArray(result[key]) ? result[key].length : 0)
  return `${String(result.files)} files, ${count('indexed')} indexed, ${String(result.unchanged)} unchanged, ${count('deleted')} deleted`
}

// "https://github.com/org/handbook" → "github.com/org/handbook"
const repoLabel = (url: string) => url.replace(/^https:\/\//, '')

interface Upload {
  name: string
  state: 'pending' | 'done' | 'error'
  message: string
  book?: string
  jobId?: number
}

export default function Documents() {
  const [docs, setDocs] = useState<DocumentRow[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [uploads, setUploads] = useState<Upload[]>([])
  const [dragging, setDragging] = useState(false)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [repoUrl, setRepoUrl] = useState('')
  const [repoRef, setRepoRef] = useState('')
  const [repoJobs, setRepoJobs] = useState<RepoJob[]>([])
  const [repoError, setRepoError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  // Bumped after every load, so polling continues even when a retry fails with the same error.
  const [loads, setLoads] = useState(0)

  const load = useCallback(async () => {
    try {
      setDocs(await listDocuments())
      setLoadError(null)
    } catch (e) {
      setLoadError(messageOf(e))
    } finally {
      setLoads((n) => n + 1)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // setTimeout after each load rather than setInterval, so slow responses never overlap.
  const busy =
    (docs ?? []).some((d) => ACTIVE.includes(d.status)) ||
    uploads.some((u) => u.state === 'pending')
  useEffect(() => {
    if (!busy) return
    const timer = setTimeout(() => void load(), POLL_MS)
    return () => clearTimeout(timer)
  }, [busy, loads, load])

  // Repository jobs index many documents with no row of their own until they are parsed, so
  // they are followed through their job; each poll replaces the list and schedules the next.
  const pendingRepos = repoJobs.filter(repoActive).map((r) => r.jobId).join(',')
  useEffect(() => {
    if (!pendingRepos) return
    const timer = setTimeout(async () => {
      const ids = pendingRepos.split(',').map(Number)
      const jobs = await Promise.all(ids.map((id) => getJob(id).catch(() => null)))
      setRepoJobs((list) =>
        list.map((r) => {
          const job = jobs.find((j) => j?.id === r.jobId)
          return job ? { ...r, status: job.status, error: job.error, result: job.result } : r
        }),
      )
      if (jobs.some((j) => j && j.status !== 'queued' && j.status !== 'running')) void load()
    }, POLL_MS)
    return () => clearTimeout(timer)
  }, [pendingRepos, repoJobs, load])

  async function submitRepo(event: FormEvent) {
    event.preventDefault()
    const url = repoUrl.trim()
    try {
      const { job } = await indexRepo(url, repoRef.trim() || undefined)
      setRepoJobs((list) => [
        { jobId: job.id, url, status: job.status, error: job.error, result: null },
        ...list,
      ])
      setRepoError(null)
      setRepoUrl('')
      setRepoRef('')
    } catch (e) {
      setRepoError(messageOf(e))
    }
  }

  const update = (name: string, changes: Partial<Upload>) =>
    setUploads((list) => list.map((u) => (u.name === name ? { ...u, ...changes } : u)))

  // Settle this tab's uploads once their job is no longer active.
  useEffect(() => {
    if (!docs) return
    for (const upload of uploads) {
      if (upload.state !== 'pending' || upload.jobId === undefined) continue
      const row = docs.find((d) => d.book === upload.book)
      if (row && ACTIVE.includes(row.status)) continue
      if (row) {
        update(
          upload.name,
          row.status === 'indexed'
            ? { state: 'done', message: 'indexed' }
            : { state: 'error', message: row.error ?? `${row.status}` },
        )
      } else {
        // The worker failed before writing the manifest: only the job knows why.
        getJob(upload.jobId)
          .then((job) =>
            update(
              upload.name,
              job.status === 'done'
                ? { state: 'done', message: 'indexed' }
                : { state: 'error', message: job.error ?? job.status },
            ),
          )
          .catch((e) => update(upload.name, { state: 'error', message: messageOf(e) }))
      }
    }
    // Runs on new document data only; uploads are read at that moment.
  }, [docs])

  async function uploadAll(files: File[]) {
    setUploads((list) => [
      ...files.map((f) => ({ name: f.name, state: 'pending' as const, message: 'uploading…' })),
      ...list.filter((u) => !files.some((f) => f.name === u.name)),
    ])
    // One at a time: the worker indexes sequentially anyway, and parallel uploads of colliding
    // names would race the API's conflict checks.
    for (const file of files) {
      try {
        const result = await uploadDocument(file)
        update(
          file.name,
          result.job
            ? {
                state: 'pending',
                message: `queued (job ${result.job.id})`,
                book: result.book,
                jobId: result.job.id,
              }
            : { state: 'done', message: 'already indexed', book: result.book },
        )
      } catch (e) {
        const message = e instanceof ApiError ? e.message : `upload failed: ${messageOf(e)}`
        update(file.name, { state: 'error', message })
      }
      await load()
    }
  }

  async function remove(book: string) {
    setConfirming(null)
    try {
      await deleteDocument(book)
      setDeleteError(null)
    } catch (e) {
      setDeleteError(messageOf(e))
    }
    await load()
  }

  function onPick(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? [])
    event.target.value = ''
    if (files.length) void uploadAll(files)
  }

  function onDrop(event: DragEvent) {
    event.preventDefault()
    setDragging(false)
    const files = Array.from(event.dataTransfer.files)
    if (files.length) void uploadAll(files)
  }

  return (
    <section>
      <h1>Documents</h1>
      <label
        className={dragging ? 'dropzone dragging' : 'dropzone'}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <input
          type="file"
          multiple
          accept=".pdf,.md,.markdown"
          className="visually-hidden"
          onChange={onPick}
        />
        Drop PDF or Markdown files here, or <span className="link">browse</span>
      </label>

      <form className="ask-controls" onSubmit={submitRepo}>
        <label htmlFor="repo-url">Index a Git repository</label>
        <input
          id="repo-url"
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
          placeholder="https://github.com/org/handbook"
        />
        <label htmlFor="repo-ref">Branch or tag</label>
        <input
          id="repo-ref"
          value={repoRef}
          onChange={(e) => setRepoRef(e.target.value)}
          placeholder="default"
        />
        <button type="submit" disabled={!repoUrl.trim()}>
          Index
        </button>
      </form>
      {repoError && <p className="error-box">{repoError}</p>}
      {repoJobs.length > 0 && (
        <ul className="uploads" aria-label="Repositories">
          {repoJobs.map((r) => (
            <li key={r.jobId} className={r.status === 'failed' ? 'error-text' : undefined}>
              <strong>{repoLabel(r.url)}</strong> job {r.jobId} {r.status}
              {r.status === 'done' && `: ${repoSummary(r.result)}`}
              {r.error && ` ${r.error}`}
            </li>
          ))}
        </ul>
      )}

      {uploads.length > 0 && (
        <ul className="uploads">
          {uploads.map((u) => (
            <li key={u.name} className={u.state === 'error' ? 'error-text' : undefined}>
              <strong>{u.name}</strong> {u.message}
            </li>
          ))}
        </ul>
      )}

      {loadError && <p className="error-box">{loadError}</p>}
      {deleteError && <p className="error-box">{deleteError}</p>}
      {docs && docs.length === 0 && (
        <p className="muted">No documents yet. Drop a PDF or Markdown file above.</p>
      )}
      {docs && docs.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Document</th>
                <th>Title</th>
                <th>Status</th>
                <th>Pages</th>
                <th>Chunks</th>
                <th>Updated</th>
                <th>
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {docs.map((d) => (
                <tr key={d.book}>
                  <td>
                    {d.book}
                    {d.file && <div className="muted small">{d.file}</div>}
                    {d.repo && <div className="muted small">from {repoLabel(d.repo)}</div>}
                  </td>
                  <td>{d.title ?? '-'}</td>
                  <td>
                    <span className={`status status-${d.status ?? 'none'}`}>
                      {d.status ?? '-'}
                    </span>
                    {d.status === 'failed' && d.error && (
                      <div className="error-text small">{d.error}</div>
                    )}
                  </td>
                  <td>{d.pages ?? '-'}</td>
                  <td>{d.chunks ?? '-'}</td>
                  <td>{formatTime(d.updated_at)}</td>
                  <td>
                    {confirming === d.book ? (
                      <div className="small">
                        Delete {d.book}? Its chunks, entities and relations go; entities it shares
                        with other documents are rebuilt from them, which drops manual graph edits
                        on them.{' '}
                        <button type="button" onClick={() => void remove(d.book)}>
                          Yes
                        </button>{' '}
                        <button type="button" onClick={() => setConfirming(null)}>
                          No
                        </button>
                      </div>
                    ) : (
                      !ACTIVE.includes(d.status) && (
                        <button
                          type="button"
                          aria-label={`Delete ${d.book}`}
                          onClick={() => setConfirming(d.book)}
                        >
                          Delete
                        </button>
                      )
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

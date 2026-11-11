import { useAccess } from '../access'
import { toast } from 'sonner'
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
import { FileUp, GitBranch, Loader2 } from 'lucide-react'
import Empty from '@/components/Empty'
import Loading from '@/components/Loading'
import ErrorAlert from '@/components/ErrorAlert'
import PageHeader from '@/components/PageHeader'
import RepoTable from '@/components/RepoTable'
import StatusBadge from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'
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
  // plans/rbac.md: maintainers upload, index and delete; a private repository's token: admins.
  const access = useAccess()
  const canEdit = access.can('maintainer')
  const [confirming, setConfirming] = useState<string | null>(null)
  const [repoUrl, setRepoUrl] = useState('')
  const [repoRef, setRepoRef] = useState('')
  const [repoToken, setRepoToken] = useState('')
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
  const pendingRepos = repoJobs
    .filter(repoActive)
    .map((r) => r.jobId)
    .join(',')
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
      const { job } = await indexRepo(
        url,
        repoRef.trim() || undefined,
        repoToken.trim() || undefined,
      )
      setRepoJobs((list) => [
        { jobId: job.id, url, status: job.status, error: job.error, result: null },
        ...list,
      ])
      setRepoError(null)
      toast.success(`Indexing ${repoLabel(url)} (job ${job.id})`)
      setRepoUrl('')
      setRepoRef('')
      setRepoToken('')
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
        if (result.job) toast.success(`${file.name} queued for indexing`)
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
      toast.success(`Deleting ${book}`)
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
      <PageHeader
        title="Documents"
        description={
          canEdit
            ? 'Upload files or index a Git repository. Everything here becomes searchable on the Chat page once it is indexed.'
            : `The documents and repositories indexed in this workspace. Uploading and indexing need the maintainer role (yours: ${access.role}).`
        }
        action={
          busy && (
            <span role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> Indexing…
            </span>
          )
        }
      />

      {canEdit && (
        <div className="mb-8 grid gap-4 md:grid-cols-2">
          <label
            className={cn(
              'flex min-h-44 cursor-pointer flex-col items-center justify-center gap-1.5 rounded-md border-[1.5px] border-dashed border-input bg-card px-5 py-8 text-center text-muted-foreground transition-colors hover:border-ring has-[input:focus-visible]:ring-[3px] has-[input:focus-visible]:ring-ring/25',
              dragging && 'border-ring bg-info-bg',
            )}
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
              className="sr-only"
              onChange={onPick}
            />
            <FileUp className="mb-1 size-6" />
            <strong className="text-base font-medium text-foreground">
              Drop PDF or Markdown files here
            </strong>
            <span>
              or <span className="text-link">browse</span> your computer
            </span>
          </label>

          <Card>
            <CardHeader>
              <Label htmlFor="repo-url" className="text-[15px] font-semibold">
                <GitBranch className="size-4" /> Index a Git repository
              </Label>
              <CardDescription>
                Markdown files on the branch are indexed and kept in sync. A private repository
                needs a read-only access token; it is stored encrypted.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form className="flex flex-col gap-3" onSubmit={submitRepo}>
                <Input
                  id="repo-url"
                  value={repoUrl}
                  onChange={(e) => setRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/handbook"
                />
                {access.can('admin') && (
                  <div className="flex items-center gap-3">
                    <Label htmlFor="repo-token" className="shrink-0 text-muted-foreground">
                      Access token
                    </Label>
                    <Input
                      id="repo-token"
                      type="password"
                      autoComplete="off"
                      value={repoToken}
                      onChange={(e) => setRepoToken(e.target.value)}
                      placeholder="only for a private repository"
                    />
                  </div>
                )}
                <div className="flex items-center gap-3">
                  <Label htmlFor="repo-ref" className="shrink-0 text-muted-foreground">
                    Branch or tag
                  </Label>
                  <Input
                    id="repo-ref"
                    value={repoRef}
                    onChange={(e) => setRepoRef(e.target.value)}
                    placeholder="default branch"
                  />
                  <Button type="submit" disabled={!repoUrl.trim()}>
                    Index
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        </div>
      )}

      {repoError && <ErrorAlert>{repoError}</ErrorAlert>}
      {repoJobs.length > 0 && (
        <ul className="my-4 flex flex-col gap-2" aria-label="Repositories">
          {repoJobs.map((r) => (
            <li key={r.jobId} className={jobRow}>
              <strong className="font-medium">{repoLabel(r.url)}</strong> job {r.jobId}{' '}
              <StatusBadge status={r.status} />
              {r.status === 'done' && (
                <span className="text-muted-foreground">: {repoSummary(r.result)}</span>
              )}
              {r.error && <span className="text-destructive">{r.error}</span>}
            </li>
          ))}
        </ul>
      )}

      {uploads.length > 0 && (
        <ul className="my-4 flex flex-col gap-2" aria-label="Uploads">
          {uploads.map((u) => (
            <li key={u.name} className={jobRow}>
              {u.state === 'pending' ? (
                <Loader2 className="size-4 animate-spin text-info" />
              ) : (
                <StatusBadge status={u.state === 'done' ? 'done' : 'failed'}>{u.state}</StatusBadge>
              )}
              <strong className="font-medium">{u.name}</strong>
              <span
                className={
                  u.state === 'error' ? 'error-text text-destructive' : 'text-muted-foreground'
                }
              >
                {u.message}
              </span>
            </li>
          ))}
        </ul>
      )}

      <section aria-label="Repositories" className="mb-8">
        <h2 className="mt-0 mb-3">Repositories</h2>
        {/* Reloaded each time one of this tab's repository jobs finishes. */}
        <RepoTable refresh={repoJobs.filter((r) => !repoActive(r)).length} />
      </section>

      <h2 className="mt-0 mb-3">Documents</h2>
      {loadError && <ErrorAlert>{loadError}</ErrorAlert>}
      {deleteError && <ErrorAlert>{deleteError}</ErrorAlert>}
      {docs === null && !loadError && <Loading />}
      {docs && docs.length === 0 && (
        <Empty>No documents yet. Drop a PDF or Markdown file above.</Empty>
      )}
      {docs && docs.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Document</TableHead>
              <TableHead>Title</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Pages</TableHead>
              <TableHead>Chunks</TableHead>
              <TableHead>Updated</TableHead>
              <TableHead>
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {docs.map((d) => (
              <TableRow key={d.book}>
                <TableCell className="max-w-80">
                  {d.book}
                  {d.file && <div className="text-[13px] text-muted-foreground">{d.file}</div>}
                  {d.repo && (
                    <div className="text-[13px] text-muted-foreground">
                      from {repoLabel(d.repo)}
                    </div>
                  )}
                </TableCell>
                <TableCell>{d.title ?? '-'}</TableCell>
                <TableCell>
                  <StatusBadge status={d.status ?? 'none'}>{d.status ?? '-'}</StatusBadge>
                  {d.status === 'failed' && d.error && (
                    <div className="mt-1 text-[13px] text-destructive">{d.error}</div>
                  )}
                </TableCell>
                <TableCell>{d.pages ?? '-'}</TableCell>
                <TableCell>{d.chunks ?? '-'}</TableCell>
                <TableCell className="whitespace-nowrap">{formatTime(d.updated_at)}</TableCell>
                <TableCell className="text-right">
                  {confirming === d.book ? (
                    <div className="ml-auto max-w-sm min-w-64 rounded-md bg-warning-bg p-3 text-left text-[13px] text-warning">
                      <p>
                        Delete {d.book}? Its chunks, entities and relations go; entities it shares
                        with other documents are rebuilt from them, which drops manual graph edits
                        on them.
                      </p>
                      <div className="mt-2 flex gap-2">
                        <Button
                          type="button"
                          size="sm"
                          variant="soft-destructive"
                          onClick={() => void remove(d.book)}
                        >
                          Yes
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="secondary"
                          onClick={() => setConfirming(null)}
                        >
                          No
                        </Button>
                      </div>
                    </div>
                  ) : (
                    canEdit &&
                    !ACTIVE.includes(d.status) && (
                      <Button
                        type="button"
                        size="sm"
                        variant="soft-destructive"
                        aria-label={`Delete ${d.book}`}
                        onClick={() => setConfirming(d.book)}
                      >
                        Delete
                      </Button>
                    )
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  )
}

const jobRow = 'flex flex-wrap items-center gap-2 rounded-md bg-card px-3.5 py-2.5 text-sm'

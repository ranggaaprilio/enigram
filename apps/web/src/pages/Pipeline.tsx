import { useAccess } from '../access'
import { toast } from 'sonner'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError,
  deleteDocument,
  getJob,
  getRepoStats,
  listActiveJobs,
  listDocuments,
  messageOf,
  uploadDocument,
  type DocumentRow,
  type Job,
  type RepoStat,
} from '../api'
import { Plus, Search } from 'lucide-react'
import Empty from '@/components/Empty'
import Loading from '@/components/Loading'
import ErrorAlert from '@/components/ErrorAlert'
import PageHeader from '@/components/PageHeader'
import AddSourceSheet, { type Upload } from '@/components/pipeline/AddSourceSheet'
import SourceDetailSheet from '@/components/pipeline/SourceDetailSheet'
import SourceRow from '@/components/pipeline/SourceRow'
import {
  ACTIVE_DOC,
  buildSources,
  isActive,
  nameOf,
  type Source,
} from '@/components/pipeline/sources'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

const POLL_MS = 3000

type Filter = 'all' | 'repo' | 'file'

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'repo', label: 'Repositories' },
  { id: 'file', label: 'Files' },
]

const matches = (s: Source, q: string) =>
  [nameOf(s), s.kind === 'repo' ? s.url : s.doc.book, s.kind === 'file' ? s.doc.file : '']
    .join(' ')
    .toLowerCase()
    .includes(q)

// Everything that feeds this workspace: Git repositories (synced by the worker, stage by stage)
// and uploaded files. A row opens its details; "Add source" opens the forms.
export default function Pipeline() {
  // plans/rbac.md: maintainers upload, index and delete; a private repository's token: admins.
  const access = useAccess()
  const canEdit = access.can('maintainer')
  const [docs, setDocs] = useState<DocumentRow[] | null>(null)
  const [jobs, setJobs] = useState<Job[]>([]) // queued and running, from GET /jobs
  const [repos, setRepos] = useState<RepoStat[]>([])
  const [finished, setFinished] = useState<Record<string, Job>>({}) // by repository URL
  const [loadError, setLoadError] = useState<string | null>(null)
  const [repoError, setRepoError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [uploads, setUploads] = useState<Upload[]>([])
  const [adding, setAdding] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  // Bumped after every load, so polling continues even when a retry fails with the same error.
  const [loads, setLoads] = useState(0)
  // Repository jobs seen queued or running (id → URL): when one leaves GET /jobs, its outcome is
  // read once and kept on its row.
  const followed = useRef(new Map<number, string>())

  const loadRepos = useCallback(async () => {
    try {
      setRepos(await getRepoStats())
      setRepoError(null)
    } catch (e) {
      setRepoError(messageOf(e))
    }
  }, [])

  const settle = useCallback(
    async (active: Job[]) => {
      for (const job of active)
        if (job.kind === 'index_repo' && typeof job.payload.url === 'string')
          followed.current.set(job.id, job.payload.url)
      const ids = new Set(active.map((j) => j.id))
      const gone = [...followed.current].filter(([id]) => !ids.has(id))
      if (!gone.length) return
      for (const [id] of gone) followed.current.delete(id)
      const ended = await Promise.all(gone.map(([id]) => getJob(id).catch(() => null)))
      const byUrl = Object.fromEntries(ended.flatMap((job, i) => (job ? [[gone[i][1], job]] : [])))
      setFinished((current) => ({ ...current, ...byUrl }))
      void loadRepos()
    },
    [loadRepos],
  )

  const load = useCallback(async () => {
    try {
      const [rows, active] = await Promise.all([listDocuments(), listActiveJobs()])
      setDocs(rows)
      setJobs(active)
      setLoadError(null)
      await settle(active)
    } catch (e) {
      setLoadError(messageOf(e))
    } finally {
      setLoads((n) => n + 1)
    }
  }, [settle])

  useEffect(() => {
    void load()
    void loadRepos()
  }, [load, loadRepos])

  // setTimeout after each load rather than setInterval, so slow responses never overlap.
  const busy =
    jobs.length > 0 ||
    (docs ?? []).some((d) => ACTIVE_DOC.includes(d.status)) ||
    uploads.some((u) => u.state === 'pending')
  useEffect(() => {
    if (!busy) return
    const timer = setTimeout(() => void load(), POLL_MS)
    return () => clearTimeout(timer)
  }, [busy, loads, load])

  function repoQueued(job: Job, url: string) {
    followed.current.set(job.id, url)
    setJobs((list) => [...list.filter((j) => j.id !== job.id), job])
    setFinished(({ [url]: _, ...rest }) => rest)
  }

  const update = (name: string, changes: Partial<Upload>) =>
    setUploads((list) => list.map((u) => (u.name === name ? { ...u, ...changes } : u)))

  // Settle this tab's uploads once their job is no longer active.
  useEffect(() => {
    if (!docs) return
    for (const upload of uploads) {
      if (upload.state !== 'pending' || upload.jobId === undefined) continue
      const row = docs.find((d) => d.book === upload.book)
      if (row && ACTIVE_DOC.includes(row.status)) continue
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
    try {
      await deleteDocument(book)
      setDeleteError(null)
      toast.success(`Deleting ${book}`)
    } catch (e) {
      setDeleteError(messageOf(e))
    }
    await load()
  }

  const sources = useMemo(
    () => buildSources(docs ?? [], repos, jobs, finished),
    [docs, repos, jobs, finished],
  )
  const counts: Record<Filter, number> = {
    all: sources.length,
    repo: sources.filter((s) => s.kind === 'repo').length,
    file: sources.filter((s) => s.kind === 'file').length,
  }
  const q = query.trim().toLowerCase()
  const shown = sources.filter((s) => (filter === 'all' || s.kind === filter) && matches(s, q))
  const working = sources.filter(isActive).length
  const open = sources.find((s) => s.key === selected) ?? null

  const addButton = canEdit && (
    <Button type="button" onClick={() => setAdding(true)}>
      <Plus /> Add source
    </Button>
  )

  return (
    <section>
      <PageHeader
        title="Pipeline"
        description={
          canEdit
            ? 'The repositories and files that feed this workspace. Once indexed, they are searchable on the Chat page.'
            : `The repositories and files indexed in this workspace. Adding and deleting sources need the maintainer role (yours: ${access.role}).`
        }
        action={
          <div className="flex items-center gap-3">
            {working > 0 && (
              <span role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
                <span aria-hidden className="relative flex size-2">
                  <span className="absolute inline-flex size-full animate-ping rounded-full bg-info opacity-60 motion-reduce:animate-none" />
                  <span className="relative inline-flex size-2 rounded-full bg-info" />
                </span>
                {working} in progress
              </span>
            )}
            {addButton}
          </div>
        }
      />

      {loadError && <ErrorAlert>{loadError}</ErrorAlert>}
      {repoError && <ErrorAlert>{repoError}</ErrorAlert>}

      {docs === null && !loadError ? (
        <Loading rows={4} />
      ) : docs && sources.length === 0 ? (
        <Empty action={addButton}>
          No sources yet. Add a Git repository or upload PDF and Markdown files.
        </Empty>
      ) : (
        sources.length > 0 && (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <div role="group" aria-label="Show" className="flex gap-1 rounded-md bg-card p-1">
                {FILTERS.map(({ id, label }) => (
                  <button
                    key={id}
                    type="button"
                    aria-pressed={filter === id}
                    onClick={() => setFilter(id)}
                    className={cn(
                      'rounded-[6px] px-2.5 py-1 text-[13px] text-muted-foreground transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/25 focus-visible:outline-none',
                      filter === id && 'bg-background font-medium text-foreground shadow-xs',
                    )}
                  >
                    {label} <span className="tabular-nums">{counts[id]}</span>
                  </button>
                ))}
              </div>
              <div className="relative ml-auto w-full sm:w-64">
                <Search
                  aria-hidden
                  className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
                />
                <Input
                  aria-label="Filter sources"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Filter by name"
                  className="pl-8"
                />
              </div>
            </div>
            {shown.length > 0 ? (
              <ul
                aria-label="Sources"
                className="m-0 list-none overflow-hidden rounded-lg border p-0"
              >
                {shown.map((s) => (
                  <SourceRow
                    key={s.key}
                    source={s}
                    onOpen={(key) => {
                      setDeleteError(null)
                      setSelected(key)
                    }}
                  />
                ))}
              </ul>
            ) : (
              <p className="m-0 py-6 text-center text-muted-foreground">No source matches.</p>
            )}
          </>
        )
      )}

      {canEdit && (
        <AddSourceSheet
          open={adding}
          onOpenChange={setAdding}
          canSetToken={access.can('admin')}
          uploads={uploads}
          onFiles={(files) => void uploadAll(files)}
          onRepoQueued={repoQueued}
        />
      )}
      <SourceDetailSheet
        source={open}
        onClose={() => setSelected(null)}
        canEdit={canEdit}
        onDelete={remove}
        deleteError={deleteError}
        onRepoQueued={repoQueued}
      />
    </section>
  )
}

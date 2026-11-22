import { useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { ExternalLink, FileText, GitBranch, RefreshCw } from 'lucide-react'
import { indexRepo, messageOf, type Job } from '../../api'
import { formatTime } from '../../format'
import ErrorAlert from '@/components/ErrorAlert'
import { Commit, RemoteBadge, RunOutcome, UPDATES_LABEL } from '@/components/RepoState'
import StatusBadge from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { WsLink } from '@/workspace'
import { SourcePill } from './SourceRow'
import SyncProgress from './SyncProgress'
import {
  ACTIVE_DOC,
  nameOf,
  repoLabel,
  repoSummary,
  type FileSource,
  type RepoSource,
  type Source,
} from './sources'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-3 py-2 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="m-0 min-w-0 break-words">{children}</dd>
    </div>
  )
}

// One source in full: what is indexed, its last run, and what a maintainer can do with it.
export default function SourceDetailSheet({
  source,
  onClose,
  canEdit,
  onDelete,
  deleteError,
  onRepoQueued,
}: {
  source: Source | null
  onClose: () => void
  canEdit: boolean
  onDelete: (book: string) => Promise<void>
  deleteError: string | null
  onRepoQueued: (job: Job, url: string) => void
}) {
  const Icon = source?.kind === 'repo' ? GitBranch : FileText
  return (
    <Sheet open={source !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-md">
        {source && (
          <>
            <SheetHeader className="border-b pr-10">
              <SheetTitle className="flex items-center gap-2">
                <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                <span className="truncate">{nameOf(source)}</span>
              </SheetTitle>
              <SheetDescription className="flex items-center gap-2">
                <SourcePill source={source} />
                <span className="truncate">
                  {source.kind === 'repo' ? repoLabel(source.url) : source.doc.book}
                </span>
              </SheetDescription>
            </SheetHeader>
            <div className="flex flex-col gap-5 p-4">
              {source.kind === 'repo' ? (
                <RepoDetail source={source} canEdit={canEdit} onRepoQueued={onRepoQueued} />
              ) : (
                <FileDetail
                  // A new document starts with its confirmation closed.
                  key={source.key}
                  source={source}
                  canEdit={canEdit}
                  onDelete={onDelete}
                  deleteError={deleteError}
                />
              )}
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}

function RepoDetail({
  source,
  canEdit,
  onRepoQueued,
}: {
  source: RepoSource
  canEdit: boolean
  onRepoQueued: (job: Job, url: string) => void
}) {
  const [queuing, setQueuing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { stat, job, finished } = source

  async function reindex() {
    setQueuing(true)
    try {
      const { job } = await indexRepo(source.url, stat?.ref ?? undefined)
      setError(null)
      toast.success(`Syncing ${source.name}`)
      onRepoQueued(job, source.url)
    } catch (e) {
      setError(messageOf(e))
    } finally {
      setQueuing(false)
    }
  }

  return (
    <>
      {job && (
        <section aria-label="Current sync" className="rounded-md border p-3">
          <h3 className="m-0 text-sm font-semibold">
            {job.status === 'queued' ? 'Queued' : 'Syncing now'}
          </h3>
          <SyncProgress job={job} />
        </section>
      )}
      {!job && finished && (
        <p
          className={
            finished.status === 'done' ? 'm-0 text-sm text-success' : 'm-0 text-sm text-destructive'
          }
        >
          {finished.status === 'done'
            ? `Synced: ${repoSummary(finished.result)}`
            : (finished.error ?? `Sync ${finished.status}`)}
        </p>
      )}

      <dl className="m-0 divide-y">
        {stat ? (
          <>
            <Field label="Commit">
              <Commit repo={stat} />
            </Field>
            <Field label="Remote">
              <RemoteBadge repo={stat} />
            </Field>
            <Field label="Branch">{stat.ref ?? 'default branch'}</Field>
            <Field label="Updates">{UPDATES_LABEL[stat.updates]}</Field>
            <Field label="Last run">
              <RunOutcome run={stat.last_run} />
            </Field>
            <Field label="Size">
              {stat.files.toLocaleString('en-US')} files · {stat.symbols.toLocaleString('en-US')}{' '}
              symbols · {stat.entrypoints.toLocaleString('en-US')} entry points
            </Field>
          </>
        ) : (
          <Field label="Code graph">
            <span className="text-muted-foreground">
              {job ? 'Not built yet' : 'None: no TypeScript code was indexed'}
            </span>
          </Field>
        )}
      </dl>

      {error && <ErrorAlert>{error}</ErrorAlert>}
      <div className="flex flex-wrap gap-2">
        {canEdit && (
          <Button type="button" disabled={queuing || job !== null} onClick={() => void reindex()}>
            <RefreshCw className={queuing ? 'animate-spin' : undefined} /> Re-index now
          </Button>
        )}
        {stat && (
          <Button asChild variant="secondary">
            <WsLink to={`/pipeline/repos/${stat.id}`}>
              <ExternalLink /> Open full details
            </WsLink>
          </Button>
        )}
      </div>

      <section aria-label="Documents from this repository">
        <h3 className="mt-0 mb-2 text-sm font-semibold">
          Documents <span className="font-normal text-muted-foreground">{source.docs.length}</span>
        </h3>
        {source.docs.length === 0 ? (
          <p className="m-0 text-[13px] text-muted-foreground">No Markdown indexed from it.</p>
        ) : (
          <ul className="m-0 flex list-none flex-col divide-y rounded-md border p-0">
            {source.docs.map((d) => (
              <li key={d.book} className="flex items-center gap-2 px-3 py-2 text-[13px]">
                <span className="min-w-0 flex-1 truncate" title={d.book}>
                  {d.title ?? d.book}
                </span>
                <StatusBadge status={d.status ?? 'none'}>{d.status ?? '-'}</StatusBadge>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  )
}

function FileDetail({
  source,
  canEdit,
  onDelete,
  deleteError,
}: {
  source: FileSource
  canEdit: boolean
  onDelete: (book: string) => Promise<void>
  deleteError: string | null
}) {
  const [confirming, setConfirming] = useState(false)
  const d = source.doc
  return (
    <>
      <dl className="m-0 divide-y">
        <Field label="Status">
          <StatusBadge status={d.status ?? 'none'}>{d.status ?? '-'}</StatusBadge>
          {d.status === 'failed' && d.error && (
            <div className="mt-1 text-[13px] text-destructive">{d.error}</div>
          )}
        </Field>
        <Field label="Title">{d.title ?? '-'}</Field>
        <Field label="File">{d.file ?? '-'}</Field>
        <Field label="Pages">{d.pages ?? '-'}</Field>
        <Field label="Chunks">{d.chunks ?? '-'}</Field>
        <Field label="Updated">{formatTime(d.updated_at)}</Field>
      </dl>

      {deleteError && <ErrorAlert>{deleteError}</ErrorAlert>}
      {canEdit &&
        !ACTIVE_DOC.includes(d.status) &&
        (confirming ? (
          <div className="rounded-md bg-warning-bg p-3 text-[13px] text-warning">
            <p className="m-0">
              Delete {d.book}? Its chunks, entities and relations go; entities it shares with other
              documents are rebuilt from them, which drops manual graph edits on them.
            </p>
            <div className="mt-2 flex gap-2">
              <Button
                type="button"
                size="sm"
                variant="soft-destructive"
                onClick={() => {
                  setConfirming(false)
                  void onDelete(d.book)
                }}
              >
                Yes
              </Button>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                onClick={() => setConfirming(false)}
              >
                No
              </Button>
            </div>
          </div>
        ) : (
          <Button
            type="button"
            variant="soft-destructive"
            className="self-start"
            aria-label={`Delete ${d.book}`}
            onClick={() => setConfirming(true)}
          >
            Delete
          </Button>
        ))}
    </>
  )
}

import { ChevronRight, FileText, GitBranch } from 'lucide-react'
import { formatAgo } from '../../format'
import StatusBadge from '@/components/StatusBadge'
import { UPDATES_LABEL } from '@/components/RepoState'
import { cn } from '@/lib/utils'
import SyncProgress from './SyncProgress'
import { isActive, nameOf, repoLabel, repoSummary, type Source } from './sources'

const number = (n: number) => n.toLocaleString('en-US')

const REMOTE_LABEL = {
  current: 'Up to date',
  behind: 'Behind',
  unreachable: "Can't check",
  unchecked: 'Not checked',
} as const

// The one pill a source shows: what it is doing, or how it stands.
export function SourcePill({ source }: { source: Source }) {
  if (source.kind === 'file') {
    const { status } = source.doc
    return (
      <StatusBadge status={status ?? 'none'}>{status?.replaceAll('_', ' ') ?? '-'}</StatusBadge>
    )
  }
  if (source.job)
    return (
      <StatusBadge status={source.job.status}>
        {source.job.status === 'queued' ? 'Queued' : 'Syncing'}
      </StatusBadge>
    )
  const ended = source.finished?.status
  if (ended === 'failed' || ended === 'budget_exceeded')
    return <StatusBadge status={ended}>Sync failed</StatusBadge>
  if (source.stat)
    return (
      <StatusBadge status={source.stat.remote.status}>
        {REMOTE_LABEL[source.stat.remote.status]}
      </StatusBadge>
    )
  return <StatusBadge status="indexed">Indexed</StatusBadge>
}

function meta(source: Source): string {
  if (source.kind === 'file') {
    const d = source.doc
    return [
      d.title ? d.book : d.file,
      d.pages != null && `${d.pages} pages`,
      d.chunks != null && `${d.chunks} chunks`,
      d.updated_at && formatAgo(d.updated_at),
    ]
      .filter(Boolean)
      .join(' · ')
  }
  const { stat, docs } = source
  if (!stat) return [repoLabel(source.url), `${docs.length} docs`].join(' · ')
  return [
    stat.indexed_sha.slice(0, 7),
    `indexed ${formatAgo(stat.indexed_at)}`,
    `${number(stat.files)} files`,
    `${number(Math.max(stat.documents, docs.length))} docs`,
    `${UPDATES_LABEL[stat.updates]} updates`,
  ].join(' · ')
}

export default function SourceRow({
  source,
  onOpen,
}: {
  source: Source
  onOpen: (key: string) => void
}) {
  const Icon = source.kind === 'repo' ? GitBranch : FileText
  const active = isActive(source)
  const error =
    source.kind === 'file'
      ? source.doc.status === 'failed' && source.doc.error
      : !source.job && source.finished?.error
  const synced =
    source.kind === 'repo' && !source.job && source.finished?.status === 'done'
      ? repoSummary(source.finished.result)
      : null

  return (
    <li className="border-b last:border-b-0">
      <button
        type="button"
        onClick={() => onOpen(source.key)}
        aria-label={`Open ${nameOf(source)}`}
        className={cn(
          'group flex w-full items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-accent/60 focus-visible:bg-accent/60 focus-visible:outline-none',
          active && 'bg-info-bg/40',
        )}
      >
        <span
          className={cn(
            'mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-card text-muted-foreground',
            active && 'text-info',
          )}
        >
          <Icon className="size-4" aria-hidden />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate font-medium">{nameOf(source)}</span>
            <span className="ml-auto shrink-0">
              <SourcePill source={source} />
            </span>
          </span>
          <span className="mt-0.5 block truncate text-[13px] text-muted-foreground">
            {meta(source)}
          </span>
          {source.kind === 'repo' && source.job && <SyncProgress job={source.job} />}
          {source.kind === 'file' && active && (
            <span
              aria-hidden
              className="relative mt-2 block h-1 overflow-hidden rounded-full bg-secondary"
            >
              <span className="absolute inset-y-0 w-2/5 animate-indeterminate rounded-full bg-info/70 motion-reduce:animate-none" />
            </span>
          )}
          {error && (
            <span className="mt-1 block text-[13px] break-words text-destructive">{error}</span>
          )}
          {synced && (
            <span className="mt-1 block text-[13px] text-success duration-500 animate-in fade-in">
              Synced: {synced}
            </span>
          )}
        </span>
        <ChevronRight
          aria-hidden
          className="mt-2 size-4 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100"
        />
      </button>
    </li>
  )
}

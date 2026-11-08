import type { IndexRun, RepoStat } from '../api'
import { formatAgo, formatSecs, formatTime } from '../format'
import StatusBadge from '@/components/StatusBadge'

// A repository against its remote (plan item 6, D2b), as the worker's last poll saw it.
const REMOTE_LABEL: Record<RepoStat['remote']['status'], string> = {
  current: 'Up to date',
  behind: 'Behind',
  unreachable: "Can't check",
  unchecked: 'Not checked',
}

export const UPDATES_LABEL: Record<RepoStat['updates'], string> = {
  webhook: 'Webhook',
  poll: 'Polling',
  manual: 'Manual',
}

export function remoteHint(repo: RepoStat): string {
  const { remote } = repo
  if (remote.status === 'unchecked')
    return repo.updates === 'webhook'
      ? 'Updated by its webhook; the worker has not polled it'
      : 'The worker has not polled it yet'
  const checked = `checked ${formatAgo(remote.checked_at)}`
  if (remote.status === 'unreachable') return `${remote.error ?? 'The check failed'}; ${checked}`
  if (remote.status === 'behind')
    return `The remote is at ${remote.sha?.slice(0, 7)}, not indexed yet; ${checked}`
  return checked
}

export function RemoteBadge({ repo }: { repo: RepoStat }) {
  return (
    <span title={remoteHint(repo)}>
      <StatusBadge status={repo.remote.status}>{REMOTE_LABEL[repo.remote.status]}</StatusBadge>
    </span>
  )
}

export function Commit({ repo }: { repo: RepoStat }) {
  return (
    <>
      <code className="text-[12px]">{repo.indexed_sha.slice(0, 7)}</code>
      <div className="text-[12px] whitespace-nowrap text-muted-foreground">
        {repo.commit_at ? formatTime(repo.commit_at) : 'date unknown'}
      </div>
    </>
  )
}

export function RunOutcome({ run }: { run: IndexRun | null }) {
  if (!run) return <span className="text-muted-foreground">No job recorded</span>
  return (
    <>
      <StatusBadge status={run.status} />
      <div className="text-[12px] whitespace-nowrap text-muted-foreground">
        {formatSecs(run.secs)}
        {run.tokens ? ` · ${run.tokens.toLocaleString('en-US')} tokens` : ''}
      </div>
    </>
  )
}

import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useParams } from 'react-router'
import { ArrowLeft, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import { useAccess } from '../access'
import { ApiError, getRepoStat, indexRepo, messageOf, type RepoStatDetail } from '../api'
import { formatAgo, formatSecs, formatTime } from '../format'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
import PageHeader from '@/components/PageHeader'
import { Commit, RemoteBadge, remoteHint, RunOutcome, UPDATES_LABEL } from '@/components/RepoState'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { WsLink } from '@/workspace'

const number = (n: number | null | undefined) => (n == null ? '-' : n.toLocaleString('en-US'))
const share = (n: number | null | undefined) => (n == null ? '-' : `${Math.round(n * 100)}%`)

// What a staleness run did to the notes on the code it re-indexed (application/staleness.py).
const STALENESS_LABEL: Record<string, string> = {
  checked: 'Notes checked',
  unchanged: 'Still match the code',
  flagged_hash: 'Flagged: their code changed',
  flagged_deps: 'Flagged: what they call changed',
  relinked: 'Moved with their code',
  orphaned: 'Orphaned: their code is gone',
  conflicts: 'Contradict the code',
  conflicts_cleared: 'Conflicts cleared',
  errors: 'Errors',
}

// Plan item 6, D2b: one repository, so "when was it last updated and how complete is its index"
// is answered without SQL.
export default function RepoDetail() {
  const { id } = useParams()
  const valid = id !== undefined && /^\d+$/.test(id)
  const access = useAccess()
  const [repo, setRepo] = useState<RepoStatDetail | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [queuing, setQueuing] = useState(false)

  const load = useCallback(async () => {
    if (!valid) return
    try {
      setRepo(await getRepoStat(Number(id)))
      setError(null)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setNotFound(true)
      else setError(messageOf(e))
    }
  }, [id, valid])

  useEffect(() => {
    void load()
  }, [load])

  async function reindex() {
    if (!repo) return
    setQueuing(true)
    try {
      const { job } = await indexRepo(repo.url, repo.ref ?? undefined)
      toast.success(`Re-index queued (job ${job.id})`)
      await load()
    } catch (e) {
      setError(messageOf(e))
    } finally {
      setQueuing(false)
    }
  }

  const back = (
    <WsLink to="/documents" className="mb-4 inline-flex items-center gap-1 text-sm">
      <ArrowLeft className="size-4" /> Documents
    </WsLink>
  )
  if (!valid || notFound)
    return (
      <section>
        {back}
        <Empty>Repository {id} not found.</Empty>
      </section>
    )
  if (!repo) return error ? <ErrorAlert>{error}</ErrorAlert> : <Loading rows={6} />

  const { quality, summaries, staleness } = repo
  const skipped = Object.entries(quality.files_skipped ?? {}).filter(([, n]) => n > 0)
  return (
    <section>
      {back}
      <PageHeader
        title={repo.name}
        description={repo.url + (repo.ref ? ` · ${repo.ref}` : ' · default branch')}
        action={
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="secondary">
              <WsLink to={`/knowledge?repo=${repo.id}`} className="hover:no-underline">
                Open in Knowledge
              </WsLink>
            </Button>
            {access.can('maintainer') && (
              <Button type="button" disabled={queuing} onClick={() => void reindex()}>
                <RefreshCw /> Re-index now
              </Button>
            )}
          </div>
        }
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Fact label="Indexed commit">
          <Commit repo={repo} />
        </Fact>
        <Fact label="Last indexed">
          {formatAgo(repo.indexed_at)}
          <div className="text-[12px] text-muted-foreground">{formatTime(repo.indexed_at)}</div>
        </Fact>
        <Fact label="Against the remote">
          <RemoteBadge repo={repo} />
          <div className="text-[12px] text-muted-foreground">{remoteHint(repo)}</div>
        </Fact>
        <Fact label="Kept up to date by">
          {UPDATES_LABEL[repo.updates]}
          <div className="text-[12px] text-muted-foreground">
            {number(repo.files)} files · {number(repo.symbols)} symbols · {number(repo.entrypoints)}{' '}
            entry points · {number(repo.documents)} docs
          </div>
        </Fact>
      </div>

      <Section title="Index history">
        {repo.runs.length === 0 ? (
          <p className="m-0 text-muted-foreground">
            No index job recorded (indexed from the command line, or before jobs were kept).
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Job</TableHead>
                <TableHead>Started by</TableHead>
                <TableHead>Commit</TableHead>
                <TableHead>Outcome</TableHead>
                <TableHead className="text-right">Changed</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {repo.runs.map((r) => (
                <TableRow key={r.job_id}>
                  <TableCell className="whitespace-nowrap">
                    #{r.job_id}
                    <div className="text-[12px] text-muted-foreground">
                      {formatTime(r.created_at)}
                    </div>
                  </TableCell>
                  <TableCell>{r.trigger ?? '-'}</TableCell>
                  <TableCell>
                    {r.commit_sha ? (
                      <>
                        <code className="text-[12px]">{r.commit_sha.slice(0, 7)}</code>
                        <div className="text-[12px] whitespace-nowrap text-muted-foreground">
                          {formatTime(r.commit_at)}
                        </div>
                      </>
                    ) : (
                      '-'
                    )}
                  </TableCell>
                  <TableCell>
                    <RunOutcome run={r} />
                    {r.error && (
                      <div className="mt-1 max-w-md text-[12px] text-destructive [overflow-wrap:anywhere]">
                        {r.error}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-right text-[12px] whitespace-nowrap tabular-nums">
                    {r.files_changed == null
                      ? '-'
                      : `${number(r.files_changed)} files · ${number(r.symbols_changed)} symbols`}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <div className="grid gap-x-8 md:grid-cols-2">
        <Section title="Graph quality">
          <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13px]">
            <Term label="Frameworks found">
              {Object.entries(quality.adapters ?? {})
                .map(([name, n]) => `${name} (${n} entry points)`)
                .join(', ') || (quality.fallback ? `none, generic ${quality.fallback}` : 'none')}
            </Term>
            {(quality.unmatched_frameworks?.length ?? 0) > 0 && (
              <Term label="Not understood">{quality.unmatched_frameworks!.join(', ')}</Term>
            )}
            <Term label="Calls resolved">{share(quality.calls_resolved_share)}</Term>
            <Term label="Calls inside the repo">{share(quality.calls_in_repo_share)}</Term>
            <Term label="Files skipped">
              {skipped.length ? skipped.map(([kind, n]) => `${n} ${kind}`).join(', ') : 'none'}
            </Term>
            {(quality.secrets_redacted ?? 0) > 0 && (
              <Term label="Secrets redacted">{number(quality.secrets_redacted)}</Term>
            )}
          </dl>
          {(quality.diagnostics_sample?.length ?? 0) > 0 && (
            <details className="mt-3 text-[13px]">
              <summary className="cursor-pointer text-muted-foreground">
                TypeScript diagnostics (sample)
              </summary>
              <ul className="mt-2 mb-0 pl-5 font-mono text-[12px] [overflow-wrap:anywhere]">
                {quality.diagnostics_sample!.map((d, i) => (
                  <li key={i}>{d}</li>
                ))}
              </ul>
            </details>
          )}
        </Section>

        <Section title="Summaries">
          {summaries ? (
            <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13px]">
              <Term label="Entry point flows">
                {number(summaries.flows)} of {number(summaries.entrypoints)}
                {summaries.capped_flows ? ` (${number(summaries.capped_flows)} cut short)` : ''}
              </Term>
              <Term label="Symbol summaries">{number(summaries.symbols)}</Term>
              <Term label="From the cache">{number(summaries.cached)}</Term>
              <Term label="Unresolved calls in flows">
                {share(summaries.unresolved_share_in_flows)}
              </Term>
              <Term label="Last run">
                {formatSecs(summaries.secs ?? null)},{' '}
                {number((summaries.tokens_in ?? 0) + (summaries.tokens_out ?? 0))} tokens
              </Term>
            </dl>
          ) : (
            <p className="m-0 text-muted-foreground">No summaries recorded.</p>
          )}
        </Section>
      </div>

      <Section title="Effect on notes, last run">
        {staleness ? (
          <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13px]">
            {Object.keys(STALENESS_LABEL)
              .filter((k) => k in staleness)
              .map((k) => (
                <Term key={k} label={STALENESS_LABEL[k]}>
                  {number(staleness[k])}
                </Term>
              ))}
          </dl>
        ) : (
          <p className="m-0 text-muted-foreground">No finished index run recorded.</p>
        )}
        {staleness && (staleness.flagged_hash || staleness.flagged_deps || staleness.orphaned) ? (
          <p className="mt-3 mb-0 text-[13px]">
            <WsLink to="/review">Open the Inbox</WsLink> to review the flagged notes.
          </p>
        ) : null}
      </Section>
    </section>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-md bg-card px-4 py-3">
      <span className="text-[13px] text-muted-foreground">{label}</span>
      <div className="min-w-0 [overflow-wrap:anywhere]">{children}</div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="mt-8">
      <h2 className="mt-0 mb-3">{title}</h2>
      {children}
    </section>
  )
}

function Term({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="m-0 tabular-nums">{children}</dd>
    </>
  )
}

import { useEffect, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router'
import { useAccess } from '../access'
import {
  getRepoStats,
  getStats,
  getWorkspaceStats,
  messageOf,
  NOTE_TYPES,
  type NoteType,
  type RepoStat,
  type Stats,
  type WorkspaceStats,
} from '../api'
import { formatAgo, formatSecs } from '../format'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
import PageHeader from '@/components/PageHeader'
import { Commit, RemoteBadge, RunOutcome, UPDATES_LABEL } from '@/components/RepoState'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { WsLink } from '@/workspace'

const WINDOWS = [7, 30, 90] as const
const ORIGINS = ['code', 'summary', 'document', 'human', 'unsupported'] as const
const ORIGIN_LABEL: Record<(typeof ORIGINS)[number], string> = {
  code: 'Code',
  summary: 'Code summary',
  document: 'Document',
  human: 'Team note',
  unsupported: 'Unsupported',
}
const TARGET_LABEL: Record<string, string> = {
  code: 'Code',
  docs: 'Documents',
  'code+docs': 'Code and documents',
}
const METHOD_LABEL: Record<string, string> = {
  rule: 'by rule',
  choice: 'by the model',
  unsure: 'unsure, asked both',
  forced: 'chosen by the asker',
}

const number = (n: number) => n.toLocaleString('en-US')
const percent = (part: number, whole: number) =>
  whole ? `${Math.round((part / whole) * 100)}%` : '-'

// Every UTC day of the window, oldest first, so a quiet day shows as an empty slot.
function windowDays(since: string, days: number): string[] {
  const start = Date.parse(`${since}T00:00:00Z`)
  return Array.from({ length: days }, (_, i) =>
    new Date(start + i * 86_400_000).toISOString().slice(0, 10),
  )
}

// Plan item 6, D2: one workspace at a glance, for admins (cost) and PM/QA (answers, review
// coverage). The panel order follows that: what needs a person first, cost last.
export default function Dashboard() {
  const [params, setParams] = useSearchParams()
  const asked = Number(params.get('days'))
  const days = (WINDOWS as readonly number[]).includes(asked) ? asked : 30
  const access = useAccess()
  const [stats, setStats] = useState<Stats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    setError(null)
    getStats(days)
      .then((s) => current && setStats(s))
      .catch((e) => current && setError(messageOf(e)))
    return () => {
      current = false
    }
  }, [days])

  const fresh = stats?.days === days ? stats : null
  return (
    <section>
      <PageHeader
        title="Dashboard"
        description="What waits for a person, how well questions are answered, how much of the code the team has checked, and what it costs."
        action={
          <div role="group" aria-label="Period" className="flex gap-1">
            {WINDOWS.map((d) => (
              <Button
                key={d}
                type="button"
                size="sm"
                variant={d === days ? 'default' : 'secondary'}
                aria-pressed={d === days}
                onClick={() => setParams(d === 30 ? {} : { days: String(d) }, { replace: true })}
              >
                {d} days
              </Button>
            ))}
          </div>
        }
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {!fresh ? !error && <Loading rows={6} /> : <Panels stats={fresh} />}
      {fresh && access.owner && <AllWorkspaces days={days} current={fresh.workspace} />}
    </section>
  )
}

function Panels({ stats }: { stats: Stats }) {
  const { attention, questions, answers, review, knowledge, tokens } = stats
  const documents = Object.values(knowledge.documents).reduce((a, b) => a + b, 0)
  if (questions.asked === 0 && knowledge.repos.length === 0 && documents === 0) {
    return (
      <Empty
        action={
          <Button asChild size="sm" variant="secondary">
            <WsLink to="/documents" className="hover:no-underline">
              Add documents or a repository
            </WsLink>
          </Button>
        }
      >
        Nothing to count yet: no documents, no repositories and no questions in this workspace.
      </Empty>
    )
  }
  const days = windowDays(stats.since, stats.days)
  const inbox =
    attention.proposed +
    attention.needs_verification +
    attention.orphaned +
    attention.note_conflicts
  const jobs = attention.failed_jobs + attention.budget_exceeded_jobs
  const asked = new Map(questions.by_day.map((d) => [d.day, d]))
  const spent = new Map<string, number>()
  for (const r of tokens.by_day) spent.set(r.day, (spent.get(r.day) ?? 0) + r.tokens)
  const sentences = ORIGINS.reduce((a, o) => a + (answers.origins[o] ?? 0), 0)
  const entry = review.entrypoints
  const routed = answers.routes.reduce((a, r) => a + r.count, 0)

  return (
    <div className="flex flex-col gap-8">
      <Panel title="Needs attention">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Tile
            label="Proposals"
            value={attention.proposed}
            hint="Agent and team claims to approve"
            to="/review?show=proposed"
          />
          <Tile
            label="Re-check"
            value={attention.needs_verification}
            hint="The code changed under the note"
            to="/review?show=needs_verification"
          />
          <Tile
            label="Orphaned or conflicting"
            value={attention.orphaned + attention.note_conflicts}
            hint={`${attention.orphaned} orphaned, ${attention.note_conflicts} contradict the code`}
            to={attention.orphaned ? '/review?show=orphaned' : '/review?show=conflict'}
          />
          <Tile
            label="Failed jobs"
            value={jobs}
            hint={`Last ${attention.jobs_since_days} days; ${attention.budget_exceeded_jobs} over the token budget`}
            to="/documents"
          />
        </div>
        <p className="mt-3 mb-0 text-[13px] text-muted-foreground">
          {inbox === 0 ? 'The inbox is empty. ' : `${number(inbox)} notes in the inbox. `}
          {attention.doc_conflicts > 0 &&
            `${number(attention.doc_conflicts)} places where a document and the code disagree (found by conflict detection).`}
        </p>
      </Panel>

      <Panel title="Questions">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Tile
            label="Asked"
            value={questions.asked}
            hint={`Last ${stats.days} days`}
            to="/trace"
          />
          <Tile
            label="Not answered"
            value={percent(questions.unanswered, questions.asked)}
            hint={`${number(questions.unanswered)} found no context`}
          />
          <Tile
            label="Typical answer time"
            value={formatSecs(questions.p50_secs)}
            hint={`Slowest 5%: ${formatSecs(questions.p95_secs)}`}
          />
          <Tile label="Errors" value={questions.errors} hint="Failed before answering" />
        </div>
        <DayBars
          label="Questions per day"
          unit="questions"
          days={days}
          value={(d) => asked.get(d)?.asked ?? 0}
          detail={(d) => {
            const row = asked.get(d)
            return row && row.unanswered ? `, ${row.unanswered} not answered` : ''
          }}
        />
      </Panel>

      <Panel title="Answer quality">
        {sentences === 0 && routed === 0 ? (
          <p className="m-0 text-muted-foreground">No answers in this period.</p>
        ) : (
          <div className="grid gap-6 md:grid-cols-2">
            <div>
              <h3 className="mt-0 mb-2 text-sm font-semibold">Where answer sentences come from</h3>
              <Bars
                label="Answer sentences per origin"
                rows={ORIGINS.map((o) => ({
                  key: o,
                  label: ORIGIN_LABEL[o],
                  value: answers.origins[o] ?? 0,
                  note: percent(answers.origins[o] ?? 0, sentences),
                }))}
              />
              <p className="mt-2 mb-0 text-[13px] text-muted-foreground">
                Unsupported: a sentence no source backs.
              </p>
            </div>
            <div>
              <h3 className="mt-0 mb-2 text-sm font-semibold">What questions were sent to</h3>
              <Bars
                label="Questions per route"
                rows={answers.routes.map((r) => ({
                  key: `${r.targets} ${r.method}`,
                  label: `${TARGET_LABEL[r.targets] ?? r.targets}, ${METHOD_LABEL[r.method] ?? r.method}`,
                  value: r.count,
                  note: percent(r.count, routed),
                }))}
              />
            </div>
          </div>
        )}
      </Panel>

      <Panel title="Review coverage">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Tile
            label="Entry points checked"
            value={percent(entry.current, entry.entrypoints)}
            hint={`${number(entry.current)} of ${number(entry.entrypoints)} marked “looks right” on the current summary`}
            to="/knowledge"
          />
          <Tile
            label="Marks out of date"
            value={entry.reviewed - entry.current}
            hint="The summary changed since it was marked"
          />
        </div>
        <NotesTable notes={review.notes} />
      </Panel>

      <Panel title="Knowledge">
        <p className="mt-0 mb-3 text-[13px] text-muted-foreground">
          Documents:{' '}
          {documents === 0
            ? 'none'
            : Object.entries(knowledge.documents)
                .map(([status, n]) => `${number(n)} ${status}`)
                .join(', ')}
          {' · '}
          <WsLink to="/documents" className="underline">
            Documents
          </WsLink>
        </p>
        {knowledge.repos.length === 0 ? (
          <p className="m-0 text-muted-foreground">No repository indexed.</p>
        ) : (
          <RepoTable />
        )}
      </Panel>

      <Panel title="Token usage">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Tile label="Today" value={tokens.today} hint="UTC day, every source" />
          <Tile
            label="Left today"
            value={tokens.left_today ?? 'No cap'}
            hint={
              tokens.budget
                ? `Daily budget ${number(tokens.budget)} (${percent(tokens.today, tokens.budget)} used)`
                : 'DAILY_TOKEN_BUDGET is 0'
            }
          />
        </div>
        <DayBars
          label="Tokens per day"
          unit="tokens"
          days={days}
          value={(d) => spent.get(d) ?? 0}
          detail={(d) =>
            tokens.by_day
              .filter((r) => r.day === d)
              .map((r) => `, ${r.source} ${number(r.tokens)}`)
              .join('')
          }
        />
        {tokens.by_key && <KeyTable rows={tokens.by_key} perKey={tokens.per_key_budget} />}
      </Panel>
    </div>
  )
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <h2 className="mt-0 mb-3">{title}</h2>
      {children}
    </section>
  )
}

function Tile(props: { label: string; value: number | string; hint: string; to?: string }) {
  const value = typeof props.value === 'number' ? number(props.value) : props.value
  const body = (
    <>
      <span className="text-[13px] text-muted-foreground">{props.label}</span>
      <span className="text-2xl font-semibold tabular-nums">{value}</span>
      <span className="text-[12px] text-muted-foreground">{props.hint}</span>
    </>
  )
  const box = 'flex min-w-0 flex-col gap-1 rounded-md bg-card px-4 py-3'
  return props.to ? (
    <WsLink
      to={props.to}
      className={cn(box, 'text-foreground hover:bg-secondary hover:no-underline')}
    >
      {body}
    </WsLink>
  ) : (
    <div className={box}>{body}</div>
  )
}

// One series per day as thin bars from the baseline; hover a day, or focus the chart and use
// the arrow keys, to read it. A single
// series needs no legend: the label names it.
function DayBars(props: {
  label: string
  unit: string
  days: string[]
  value: (day: string) => number
  detail: (day: string) => string
}) {
  const [shown, setShown] = useState<string | null>(null)
  const values = props.days.map(props.value)
  const max = Math.max(...values, 1)
  const total = values.reduce((a, b) => a + b, 0)
  // The hovered or focused day, else the whole period.
  const readout = shown
    ? `${shown}: ${number(props.value(shown))} ${props.unit}${props.detail(shown)}`
    : `${number(total)} ${props.unit} in ${props.days.length} days`

  return (
    <figure className="mt-4 mb-0">
      <figcaption className="mb-2 flex flex-wrap justify-between gap-2 text-[13px]">
        <span className="font-semibold">{props.label}</span>
        <span className="text-muted-foreground tabular-nums" aria-live="polite">
          {total === 0 ? `No ${props.unit} in this period` : readout}
        </span>
      </figcaption>
      {/* One Tab stop: the arrow keys (Home, End) move between days, read out above. */}
      <div
        role="list"
        tabIndex={0}
        aria-label={`${props.label}, arrow keys move between days`}
        className="flex h-24 items-end gap-[2px] rounded-sm border-b outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
        onMouseLeave={() => setShown(null)}
        onFocus={() => setShown((now) => now ?? props.days[props.days.length - 1])}
        onBlur={() => setShown(null)}
        onKeyDown={(e) => {
          const at = shown ? props.days.indexOf(shown) : props.days.length - 1
          const to = { ArrowLeft: at - 1, ArrowRight: at + 1, Home: 0, End: props.days.length - 1 }[
            e.key
          ]
          if (to === undefined) return
          e.preventDefault()
          setShown(props.days[Math.min(Math.max(to, 0), props.days.length - 1)])
        }}
      >
        {props.days.map((day, i) => (
          <div
            key={day}
            role="listitem"
            aria-label={`${day}: ${number(values[i])} ${props.unit}`}
            className="group flex h-full min-w-0 flex-1 items-end"
            onMouseEnter={() => setShown(day)}
          >
            <div
              className={cn(
                'w-full rounded-t-[4px] bg-foreground/70 group-hover:bg-foreground',
                shown === day && 'bg-foreground',
              )}
              style={{ height: values[i] ? `${Math.max((values[i] / max) * 100, 3)}%` : 0 }}
            />
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[12px] text-muted-foreground tabular-nums">
        <span>{props.days[0]}</span>
        <span>{props.days[props.days.length - 1]}</span>
      </div>
    </figure>
  )
}

// Horizontal bars of one measure, each labelled with its name and value (never colour alone).
function Bars(props: {
  label: string
  rows: { key: string; label: string; value: number; note: string }[]
}) {
  const max = Math.max(...props.rows.map((r) => r.value), 1)
  return (
    <ul aria-label={props.label} className="m-0 flex list-none flex-col gap-2 p-0">
      {props.rows.map((r) => (
        <li key={r.key} className="text-[13px]">
          <div className="mb-0.5 flex justify-between gap-2">
            <span>{r.label}</span>
            <span className="text-muted-foreground tabular-nums">
              {number(r.value)} · {r.note}
            </span>
          </div>
          <div className="h-2 rounded-[4px] bg-secondary">
            <div
              className="h-full rounded-[4px] bg-foreground/70"
              style={{ width: `${(r.value / max) * 100}%` }}
            />
          </div>
        </li>
      ))}
    </ul>
  )
}

// Plan item 6, D2b: each repository's commit, last index and state against its remote; a name
// opens its detail page.
function RepoTable() {
  const [repos, setRepos] = useState<RepoStat[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    getRepoStats()
      .then((r) => current && setRepos(r))
      .catch((e) => current && setError(messageOf(e)))
    return () => {
      current = false
    }
  }, [])

  if (error) return <ErrorAlert>{error}</ErrorAlert>
  if (!repos) return <Loading rows={2} />
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Repository</TableHead>
          <TableHead>Commit</TableHead>
          <TableHead>Indexed</TableHead>
          <TableHead>Remote</TableHead>
          <TableHead>Last index</TableHead>
          <TableHead className="text-right">Size</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {repos.map((r) => (
          <TableRow key={r.id}>
            <TableCell>
              <WsLink to={`/dashboard/repos/${r.id}`}>{r.name}</WsLink>
              <div className="text-[12px] text-muted-foreground">
                {UPDATES_LABEL[r.updates]} updates
              </div>
            </TableCell>
            <TableCell>
              <Commit repo={r} />
            </TableCell>
            <TableCell className="whitespace-nowrap">{formatAgo(r.indexed_at)}</TableCell>
            <TableCell>
              <RemoteBadge repo={r} />
            </TableCell>
            <TableCell>
              <RunOutcome run={r.last_run} />
            </TableCell>
            <TableCell className="text-right text-[12px] whitespace-nowrap text-muted-foreground tabular-nums">
              {number(r.files)} files · {number(r.symbols)} symbols
              <div>
                {number(r.entrypoints)} entry points · {number(r.documents)} docs
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

// Plan item 6, D3: every workspace against the daily budget (DAILY_TOKEN_BUDGET applies to each
// one), for an owner; a name opens that workspace's Dashboard for the same period.
function AllWorkspaces({ days, current }: { days: number; current: string }) {
  const [all, setAll] = useState<WorkspaceStats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setError(null)
    getWorkspaceStats(days)
      .then((s) => live && setAll(s))
      .catch((e) => live && setError(messageOf(e)))
    return () => {
      live = false
    }
  }, [days])

  const fresh = all?.days === days ? all : null
  return (
    <div className="mt-8">
      <Panel title="All workspaces">
        <p className="mt-0 mb-3 text-[13px] text-muted-foreground">
          Owners only.{' '}
          {fresh?.budget
            ? `Each workspace may spend ${number(fresh.budget)} tokens a UTC day.`
            : 'No daily token cap (DAILY_TOKEN_BUDGET is 0).'}
        </p>
        {error && <ErrorAlert>{error}</ErrorAlert>}
        {!fresh ? (
          !error && <Loading rows={2} />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Workspace</TableHead>
                <TableHead className="text-right">Tokens today</TableHead>
                <TableHead className="text-right">Tokens, {days} days</TableHead>
                <TableHead className="text-right">Questions</TableHead>
                <TableHead className="text-right">Waiting notes</TableHead>
                <TableHead className="text-right">Failed jobs</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {fresh.workspaces.map((w) => (
                <TableRow key={w.workspace}>
                  <TableCell>
                    <Link
                      to={`/w/${w.workspace}/dashboard${days === 30 ? '' : `?days=${days}`}`}
                      aria-current={w.workspace === current ? 'page' : undefined}
                    >
                      {w.workspace}
                    </Link>
                  </TableCell>
                  <TableCell className="text-right whitespace-nowrap tabular-nums">
                    {number(w.tokens_today)}
                    {fresh.budget && (
                      <div className="text-[12px] text-muted-foreground">
                        {percent(w.tokens_today, fresh.budget)} of the budget
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {number(w.tokens_period)}
                  </TableCell>
                  <TableCell className="text-right whitespace-nowrap tabular-nums">
                    {number(w.asked)}
                    {w.unanswered > 0 && (
                      <div className="text-[12px] text-muted-foreground">
                        {number(w.unanswered)} not answered
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{number(w.waiting)}</TableCell>
                  <TableCell className="text-right tabular-nums">{number(w.failed_jobs)}</TableCell>
                </TableRow>
              ))}
              <TableRow className="font-semibold">
                <TableCell>All</TableCell>
                <TableCell className="text-right tabular-nums">
                  {number(fresh.total.tokens_today)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {number(fresh.total.tokens_period)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {number(fresh.total.asked)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {number(fresh.total.waiting)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {number(fresh.total.failed_jobs)}
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        )}
      </Panel>
    </div>
  )
}

const STATUS_ORDER = ['verified', 'proposed', 'needs_verification', 'orphaned', 'rejected']

function NotesTable({ notes }: { notes: Stats['review']['notes'] }) {
  if (notes.length === 0)
    return <p className="mt-4 mb-0 text-muted-foreground">No notes in this workspace yet.</p>
  const types = [...new Set(notes.map((n) => n.note_type))]
  const statuses = STATUS_ORDER.filter((s) => notes.some((n) => n.status === s))
  const count = (t: NoteType, s: string) =>
    notes.find((n) => n.note_type === t && n.status === s)?.count ?? 0
  return (
    <div className="mt-4">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Notes</TableHead>
            {statuses.map((s) => (
              <TableHead key={s} className="text-right">
                {s.replace('_', ' ')}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {types.map((t) => (
            <TableRow key={t}>
              <TableCell>{NOTE_TYPES[t] ?? t}</TableCell>
              {statuses.map((s) => (
                <TableCell key={s} className="text-right tabular-nums">
                  {number(count(t, s))}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function KeyTable({
  rows,
  perKey,
}: {
  rows: NonNullable<Stats['tokens']['by_key']>
  perKey: number | null
}) {
  const totals = new Map<string, number>()
  for (const r of rows) totals.set(r.api_key, (totals.get(r.api_key) ?? 0) + r.tokens)
  const latestDay = rows.reduce((a, r) => (r.day > a ? r.day : a), '')
  const sorted = [...totals.entries()].sort((a, b) => b[1] - a[1])
  if (sorted.length === 0) return null
  return (
    <div className="mt-4">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Key (admins only)</TableHead>
            <TableHead className="text-right">Period</TableHead>
            <TableHead className="text-right">Latest day</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {sorted.map(([key, total]) => {
            const latest = rows.find((r) => r.api_key === key && r.day === latestDay)?.tokens ?? 0
            return (
              <TableRow key={key}>
                <TableCell>{key || 'worker (no key)'}</TableCell>
                <TableCell className="text-right tabular-nums">{number(total)}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {number(latest)}
                  {perKey && key ? ` / ${number(perKey)}` : ''}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useParams } from 'react-router'
import { WsLink } from '@/workspace'
import { ArrowLeft, ChevronRight, RefreshCw } from 'lucide-react'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import PageHeader from '@/components/PageHeader'
import StatusBadge from '@/components/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  ApiError,
  getQuery,
  listQueries,
  messageOf,
  type QueryRecord,
  type QuerySummary,
  type RouteDecision,
  type Source,
} from '../api'
import Answer from '../components/Answer'
import { formatSecs, formatTime, modeLabel } from '../format'

function outcome(q: QuerySummary): ReactNode {
  if (q.error) return <StatusBadge status="failed">error</StatusBadge>
  if (q.found === false) return <StatusBadge status="queued">no context</StatusBadge>
  return <StatusBadge status="indexed">found</StatusBadge>
}

export function TraceList() {
  const [rows, setRows] = useState<QuerySummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setRows(await listQueries(50))
      setError(null)
    } catch (e) {
      setError(messageOf(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <section>
      <PageHeader
        title="Trace"
        description="Every question asked, with what was retrieved to answer it."
        action={
          <Button type="button" variant="secondary" onClick={() => void load()}>
            <RefreshCw /> Refresh
          </Button>
        }
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {rows && rows.length === 0 && <Empty>No queries yet. Ask something on the Ask page.</Empty>}
      {rows && rows.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>#</TableHead>
              <TableHead>Time</TableHead>
              <TableHead>Mode</TableHead>
              <TableHead>Result</TableHead>
              <TableHead>Duration</TableHead>
              <TableHead>Question</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((q) => (
              <TableRow key={q.id}>
                <TableCell>
                  <WsLink to={`/trace/${q.id}`}>#{q.id}</WsLink>
                </TableCell>
                <TableCell className="whitespace-nowrap">{formatTime(q.created_at)}</TableCell>
                <TableCell className="whitespace-nowrap text-muted-foreground">
                  {modeLabel(q.mode)}
                </TableCell>
                <TableCell>{outcome(q)}</TableCell>
                <TableCell className="whitespace-nowrap">{formatSecs(q.query_secs)}</TableCell>
                <TableCell className="max-w-md truncate" title={q.question}>
                  {q.question}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  )
}

// Names open the Annotations page with a new note on that entity prefilled.
const noteLink = (kind: string, ref: string) => (
  <WsLink to={`/annotations?target_kind=${kind}&target_ref=${encodeURIComponent(ref)}`}>
    {ref}
  </WsLink>
)

// A code source resolved to a graph symbol can be annotated; a human source opens its note.
function sourceLink(s: Source) {
  if (s.origin === 'code' && s.target) {
    const at = s.target.indexOf(':')
    const symbol = encodeURIComponent(s.target.slice(at + 1))
    const to = `/annotations?target_kind=symbol&repo=${s.target.slice(0, at)}&target_ref=${symbol}`
    return <WsLink to={to}>Annotate</WsLink>
  }
  if (s.origin === 'human' && s.target) {
    return <WsLink to={`/annotations?id=${s.target}`}>note#{s.target}</WsLink>
  }
  return null
}

function Section({
  title,
  count,
  children,
}: {
  title: string
  count: number
  children: ReactNode
}) {
  return (
    <Collapsible defaultOpen={count > 0 && count <= 10} className="group my-3 rounded-xl bg-card">
      <CollapsibleTrigger className="flex w-full cursor-pointer items-center gap-2 px-5 py-4 text-left font-semibold tracking-[-0.3px]">
        <ChevronRight className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        {title} ({count})
      </CollapsibleTrigger>
      {/* forceMount: closed sections stay in the DOM, as the old <details> did; Radix then
          leaves hiding to CSS on data-state. */}
      <CollapsibleContent forceMount className="px-2 pb-3 data-[state=closed]:hidden">
        {children}
      </CollapsibleContent>
    </Collapsible>
  )
}

// "route graph_flow → code (rule: http_route, flow_phrase)"
function RouteLine({ route }: { route: RouteDecision }) {
  const rules = route.rules.length ? `: ${route.rules.join(', ')}` : ''
  return (
    <Badge variant="outline" className="text-muted-foreground">
      route {route.route} → {route.targets.join(' + ')} ({route.method}
      {rules})
    </Badge>
  )
}

export function TraceDetail() {
  const { id } = useParams()
  const valid = id !== undefined && /^\d+$/.test(id)
  const [record, setRecord] = useState<QueryRecord | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setRecord(null)
    setNotFound(false)
    setError(null)
    if (!valid) return
    // A slower response for the previous id must not replace this one.
    let current = true
    getQuery(Number(id))
      .then((r) => current && setRecord(r))
      .catch((e) => {
        if (!current) return
        if (e instanceof ApiError && e.status === 404) setNotFound(true)
        else setError(messageOf(e))
      })
    return () => {
      current = false
    }
  }, [id, valid])

  const back = (
    <WsLink to="/trace" className="mb-4 inline-flex items-center gap-1 text-sm">
      <ArrowLeft className="size-4" /> All queries
    </WsLink>
  )

  if (!valid || notFound) {
    return (
      <section>
        {back}
        <Empty>Query #{id} not found.</Empty>
      </section>
    )
  }
  if (error) return <ErrorAlert>{error}</ErrorAlert>
  if (!record) return <p className="text-muted-foreground">Loading…</p>

  const result = record.result
  return (
    <section>
      {back}
      <h1 className="mb-3">{record.question}</h1>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground">
        <span>
          #{record.id} · {formatTime(record.created_at)} · {modeLabel(record.mode)} ·{' '}
          {formatSecs(record.query_secs)} · {record.source}
        </span>
        {result?.extra?.route && <RouteLine route={result.extra.route} />}
      </div>
      {record.error && <ErrorAlert>{record.error}</ErrorAlert>}
      {result && (
        <>
          <Answer result={result} />
          <div className="mt-8">
            <Section title="Chunks" count={result.chunks.length}>
              {result.chunks.map((c) => (
                <div key={c.chunk_id} className="mx-3 my-3">
                  <div className="mb-1 text-[13px] text-muted-foreground">
                    [{c.ref}]{' '}
                    <code className="rounded-sm bg-secondary px-1.5 text-[12px]">{c.chunk_id}</code>
                  </div>
                  <pre className="rounded-md bg-background px-4 py-3 text-[13px] leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
                    {c.text}
                  </pre>
                </div>
              ))}
            </Section>
            <Section title="Entities" count={result.entities.length}>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Description</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {result.entities.map((e) => (
                    <TableRow key={e.name}>
                      <TableCell>{noteLink('entity', e.name)}</TableCell>
                      <TableCell>{e.type}</TableCell>
                      <TableCell>{e.description}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Section>
            <Section title="Relations" count={result.relations.length}>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Source → Target</TableHead>
                    <TableHead>Keywords</TableHead>
                    <TableHead>Description</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {result.relations.map((r) => (
                    <TableRow key={`${r.source}\u0000${r.target}`}>
                      <TableCell>
                        {noteLink('entity', r.source)} → {noteLink('entity', r.target)}
                      </TableCell>
                      <TableCell>{r.keywords}</TableCell>
                      <TableCell>{r.description}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Section>
            <Section title="Sources" count={result.sources.length}>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Ref</TableHead>
                    <TableHead>Title</TableHead>
                    <TableHead>Section</TableHead>
                    <TableHead>Pages</TableHead>
                    <TableHead>Citation</TableHead>
                    <TableHead>Origin</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {result.sources.map((s) => (
                    <TableRow key={s.ref}>
                      <TableCell>[{s.ref}]</TableCell>
                      <TableCell>{s.title}</TableCell>
                      <TableCell>{s.section}</TableCell>
                      <TableCell>{s.pages}</TableCell>
                      <TableCell className="text-[13px] [overflow-wrap:anywhere]">
                        {s.citation}
                      </TableCell>
                      <TableCell>{s.origin}</TableCell>
                      <TableCell>{sourceLink(s)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Section>
          </div>
        </>
      )}
    </section>
  )
}

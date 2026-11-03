import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import {
  ApiError,
  getQuery,
  listQueries,
  messageOf,
  type QueryRecord,
  type QuerySummary,
  type Source,
} from '../api'
import Answer from '../components/Answer'
import { formatSecs, formatTime } from '../format'

function outcome(q: QuerySummary): ReactNode {
  if (q.error) return <span className="status status-failed">error</span>
  if (q.found === false) return <span className="status status-queued">no context</span>
  return <span className="status status-indexed">found</span>
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
      <div className="heading-row">
        <h1>Trace</h1>
        <button type="button" onClick={() => void load()}>
          Refresh
        </button>
      </div>
      {error && <p className="error-box">{error}</p>}
      {rows && rows.length === 0 && (
        <p className="muted">No queries yet. Ask something on the Ask page.</p>
      )}
      {rows && rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Time</th>
                <th>Mode</th>
                <th>Result</th>
                <th>Duration</th>
                <th>Question</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((q) => (
                <tr key={q.id}>
                  <td>
                    <Link to={`/trace/${q.id}`}>#{q.id}</Link>
                  </td>
                  <td className="nowrap">{formatTime(q.created_at)}</td>
                  <td>{q.mode}</td>
                  <td>{outcome(q)}</td>
                  <td className="nowrap">{formatSecs(q.query_secs)}</td>
                  <td className="truncate" title={q.question}>
                    {q.question}
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

// Names open the Annotations page with a new note on that entity prefilled.
const noteLink = (kind: string, ref: string) => (
  <Link to={`/annotations?target_kind=${kind}&target_ref=${encodeURIComponent(ref)}`}>{ref}</Link>
)

// A code source resolved to a graph symbol can be annotated; a human source opens its note.
function sourceLink(s: Source) {
  if (s.origin === 'code' && s.target) {
    const at = s.target.indexOf(':')
    const symbol = encodeURIComponent(s.target.slice(at + 1))
    const to = `/annotations?target_kind=symbol&repo=${s.target.slice(0, at)}&target_ref=${symbol}`
    return <Link to={to}>Annotate</Link>
  }
  if (s.origin === 'human' && s.target) {
    return <Link to={`/annotations?id=${s.target}`}>note#{s.target}</Link>
  }
  return null
}

function Section({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <details open={count <= 10}>
      <summary>
        {title} ({count})
      </summary>
      {children}
    </details>
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

  if (!valid || notFound) {
    return (
      <section>
        <p>Query #{id} not found.</p>
        <Link to="/trace">Back to all queries</Link>
      </section>
    )
  }
  if (error) return <p className="error-box">{error}</p>
  if (!record) return <p className="muted">Loading…</p>

  const result = record.result
  return (
    <section>
      <p>
        <Link to="/trace">← All queries</Link>
      </p>
      <h1>{record.question}</h1>
      <p className="muted small">
        #{record.id} · {formatTime(record.created_at)} · {record.mode} ·{' '}
        {formatSecs(record.query_secs)} · {record.source}
      </p>
      {record.error && <p className="error-box">{record.error}</p>}
      {result && (
        <>
          <Answer result={result} />
          <Section title="Chunks" count={result.chunks.length}>
            {result.chunks.map((c) => (
              <div key={c.chunk_id} className="chunk">
                <div className="small">
                  [{c.ref}] <code>{c.chunk_id}</code>
                </div>
                <pre>{c.text}</pre>
              </div>
            ))}
          </Section>
          <Section title="Entities" count={result.entities.length}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Type</th>
                    <th>Description</th>
                  </tr>
                </thead>
                <tbody>
                  {result.entities.map((e) => (
                    <tr key={e.name}>
                      <td>{noteLink('entity', e.name)}</td>
                      <td>{e.type}</td>
                      <td>{e.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
          <Section title="Relations" count={result.relations.length}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Source → Target</th>
                    <th>Keywords</th>
                    <th>Description</th>
                  </tr>
                </thead>
                <tbody>
                  {result.relations.map((r) => (
                    <tr key={`${r.source}\u0000${r.target}`}>
                      <td>
                        {noteLink('entity', r.source)} → {noteLink('entity', r.target)}
                      </td>
                      <td>{r.keywords}</td>
                      <td>{r.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
          <Section title="Sources" count={result.sources.length}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Ref</th>
                    <th>Title</th>
                    <th>Section</th>
                    <th>Pages</th>
                    <th>Citation</th>
                    <th>Origin</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {result.sources.map((s) => (
                    <tr key={s.ref}>
                      <td>[{s.ref}]</td>
                      <td>{s.title}</td>
                      <td>{s.section}</td>
                      <td>{s.pages}</td>
                      <td className="small">{s.citation}</td>
                      <td>{s.origin}</td>
                      <td>{sourceLink(s)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        </>
      )}
    </section>
  )
}

import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import {
  createAnnotation,
  getAnnotation,
  listAnnotations,
  listRepos,
  messageOf,
  patchAnnotation,
  searchSymbols,
  type Annotation,
  type AnnotationEvent,
  type AnnotationPatch,
  type RepoRow,
  type SymbolRow,
  type TargetKind,
} from '../api'
import { formatTime } from '../format'

const STATUSES = ['all', 'needs_verification', 'orphaned', 'verified', 'proposed', 'rejected']
const KINDS: TargetKind[] = ['symbol', 'entity', 'document']

// Writes are synchronous (no worker job): every action reloads the list.
export default function Annotations() {
  const [params, setParams] = useSearchParams()
  const status = params.get('status') ?? 'all'
  const openId = Number(params.get('id')) || null
  const [notes, setNotes] = useState<Annotation[] | null>(null)
  const [repos, setRepos] = useState<RepoRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [history, setHistory] = useState<Record<number, AnnotationEvent[]>>({})

  const load = useCallback(async () => {
    try {
      setNotes(
        await listAnnotations({
          status: status === 'all' ? undefined : status,
        }),
      )
    } catch (e) {
      setError(messageOf(e))
    }
  }, [status])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    listRepos()
      .then(setRepos)
      .catch(() => setRepos([]))
  }, [])

  const showHistory = useCallback(async (id: number) => {
    try {
      const note = await getAnnotation(id)
      setHistory((h) => ({ ...h, [id]: note.events }))
    } catch (e) {
      setError(messageOf(e))
    }
  }, [])

  function toggleHistory(id: number) {
    if (!(id in history)) return void showHistory(id)
    setHistory(({ [id]: _closed, ...rest }) => rest)
  }

  useEffect(() => {
    if (openId) void showHistory(openId)
  }, [openId, showHistory])

  async function change(id: number, body: AnnotationPatch) {
    try {
      await patchAnnotation(id, body)
      setError(null)
      await load()
    } catch (e) {
      setError(messageOf(e))
    }
  }

  const repoName = (id: number | null) => repos.find((r) => r.id === id)?.name ?? `repo ${id}`

  return (
    <section>
      <h1>Annotations</h1>
      <div className="ask-controls">
        <label htmlFor="status">Status</label>
        <select
          id="status"
          aria-label="Status"
          value={status}
          onChange={(e) => {
            const next = new URLSearchParams(params)
            next.set('status', e.target.value)
            setParams(next)
          }}
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s.replace('_', ' ')}
            </option>
          ))}
        </select>
      </div>
      <NewNoteForm
        repos={repos}
        initialKind={(params.get('target_kind') as TargetKind) || 'symbol'}
        initialRef={params.get('target_ref') ?? ''}
        initialRepo={Number(params.get('repo')) || null}
        onCreated={async () => {
          setError(null)
          await load()
        }}
        onError={setError}
      />
      {error && <p className="error-box">{error}</p>}
      {notes === null ? (
        <p className="muted">Loading…</p>
      ) : notes.length === 0 ? (
        <p className="muted">No annotations.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Target</th>
                <th>Note</th>
                <th>Status</th>
                <th>Owner</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {notes.map((n) => (
                <NoteRow
                  key={n.id}
                  note={n}
                  target={
                    n.target_kind === 'symbol'
                      ? `${repoName(n.repo_id)} · ${n.target_ref.split('#').pop()}`
                      : n.target_ref
                  }
                  events={history[n.id]}
                  onChange={(body) => change(n.id, body)}
                  onHistory={() => toggleHistory(n.id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="small">
        <Link to="/graph">Graph editor (structural LightRAG fixes)</Link>
      </p>
    </section>
  )
}

function NoteRow(props: {
  note: Annotation
  target: string
  events: AnnotationEvent[] | undefined
  onChange: (body: AnnotationPatch) => void
  onHistory: () => void
}) {
  const { note: n } = props
  const [confirming, setConfirming] = useState(false)
  const [relinkTo, setRelinkTo] = useState(n.candidates[0]?.id ?? '')
  return (
    <>
      <tr>
        <td title={n.target_ref}>
          <span className="chip">{n.target_kind}</span> {props.target}
        </td>
        <td>
          {n.text}
          {n.by && <div className="muted small">by {n.by}</div>}
          {n.via === 'mcp' && <div className="muted small">via MCP by {n.author}</div>}
          {n.question && <div className="muted small">Question: {n.question}</div>}
          {n.memory_id !== null && <div className="muted small">from memory #{n.memory_id}</div>}
        </td>
        <td className="nowrap">
          <span className={`status status-${n.status}`}>{n.status.replace('_', ' ')}</span>
          {n.reason && <span className="muted small"> ({n.reason})</span>}
          {n.conflict && <span className="chip chip-error">conflict</span>}
        </td>
        <td className="small">
          {n.owner}
          {n.verified_by && (
            <div className="muted">
              verified by {n.verified_by} {formatTime(n.verified_at)}
            </div>
          )}
          {n.verify_by && <div className="muted">verify by {formatTime(n.verify_by)}</div>}
        </td>
        <td className="nowrap">
          {n.status !== 'verified' && (
            <button type="button" onClick={() => props.onChange({ status: 'verified' })}>
              Verify
            </button>
          )}{' '}
          {n.status !== 'rejected' &&
            (confirming ? (
              <>
                Reject?{' '}
                <button type="button" onClick={() => props.onChange({ status: 'rejected' })}>
                  Yes
                </button>{' '}
                <button type="button" onClick={() => setConfirming(false)}>
                  No
                </button>
              </>
            ) : (
              <button type="button" onClick={() => setConfirming(true)}>
                Reject
              </button>
            ))}{' '}
          <button type="button" onClick={() => props.onChange({ conflict: !n.conflict })}>
            {n.conflict ? 'Unmark conflict' : 'Conflict'}
          </button>{' '}
          <button type="button" onClick={props.onHistory}>
            History
          </button>
          {n.status === 'orphaned' && n.candidates.length > 0 && (
            <div>
              <select
                aria-label={`Relink ${n.target_ref}`}
                value={relinkTo}
                onChange={(e) => setRelinkTo(e.target.value)}
              >
                {n.candidates.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.qualified_name} ({c.file})
                  </option>
                ))}
              </select>{' '}
              <button type="button" onClick={() => props.onChange({ target_ref: relinkTo })}>
                Relink
              </button>
            </div>
          )}
        </td>
      </tr>
      {props.events && (
        <tr>
          <td colSpan={5}>
            <ul className="small">
              {props.events.map((e) => (
                <li key={e.id}>
                  {formatTime(e.at)} · {e.actor} · {e.action}
                  {e.new && ` · ${Object.keys(e.new).join(', ')}`}
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  )
}

function NewNoteForm(props: {
  repos: RepoRow[]
  initialKind: TargetKind
  initialRef: string
  initialRepo: number | null
  onCreated: () => Promise<void>
  onError: (message: string) => void
}) {
  const [kind, setKind] = useState<TargetKind>(props.initialKind)
  const [repo, setRepo] = useState<number | null>(props.initialRepo)
  const [targetRef, setTargetRef] = useState(props.initialRef)
  const [lookup, setLookup] = useState('')
  const [found, setFound] = useState<SymbolRow[]>([])
  const [text, setText] = useState('')
  const [by, setBy] = useState('')
  const [scope, setScope] = useState<'deps' | 'body'>('deps')
  const [verifyBy, setVerifyBy] = useState('')
  const repoId = repo ?? props.repos[0]?.id ?? null

  async function find() {
    if (!repoId || !lookup.trim()) return
    try {
      setFound(await searchSymbols(repoId, lookup.trim()))
    } catch (e) {
      props.onError(messageOf(e))
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    try {
      await createAnnotation({
        target_kind: kind,
        target_ref: targetRef.trim(),
        text: text.trim(),
        ...(kind === 'symbol' && repoId ? { repo: repoId, scope } : {}),
        ...(by.trim() ? { by: by.trim() } : {}),
        ...(verifyBy ? { verify_by: `${verifyBy}T00:00:00Z` } : {}),
      })
      setText('')
      await props.onCreated()
    } catch (e) {
      props.onError(messageOf(e))
    }
  }

  return (
    <details open={Boolean(props.initialRef)}>
      <summary>New annotation</summary>
      <form className="ask-form" onSubmit={submit}>
        <div className="ask-controls">
          <label>
            Kind{' '}
            <select value={kind} onChange={(e) => setKind(e.target.value as TargetKind)}>
              {KINDS.map((k) => (
                <option key={k}>{k}</option>
              ))}
            </select>
          </label>
          {kind === 'symbol' && (
            <>
              <label>
                Repository{' '}
                <select
                  value={repoId ?? ''}
                  onChange={(e) => setRepo(Number(e.target.value) || null)}
                >
                  {props.repos.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Symbol{' '}
                <input
                  value={lookup}
                  placeholder="logout or AuthService.logout"
                  onChange={(e) => setLookup(e.target.value)}
                />
              </label>
              <button type="button" onClick={() => void find()}>
                Find
              </button>
            </>
          )}
        </div>
        {found.length > 0 && (
          <ul className="small">
            {found.map((s) => (
              <li key={s.id}>
                <button type="button" className="link" onClick={() => setTargetRef(s.id)}>
                  {s.qualified_name}
                </button>{' '}
                <span className="muted">
                  {s.kind} · {s.file}:{s.line_start}-{s.line_end}
                </span>
              </li>
            ))}
          </ul>
        )}
        <label>
          Target
          <input
            value={targetRef}
            placeholder="symbol id, qualified name, entity or document name"
            onChange={(e) => setTargetRef(e.target.value)}
          />
        </label>
        <label>
          Note
          <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} />
        </label>
        <div className="ask-controls">
          <label>
            By <input value={by} onChange={(e) => setBy(e.target.value)} />
          </label>
          {kind === 'symbol' && (
            <label>
              Scope{' '}
              <select value={scope} onChange={(e) => setScope(e.target.value as 'deps' | 'body')}>
                <option value="deps">body and callees</option>
                <option value="body">body only</option>
              </select>
            </label>
          )}
          <label>
            Verify by{' '}
            <input type="date" value={verifyBy} onChange={(e) => setVerifyBy(e.target.value)} />
          </label>
          <button type="submit" disabled={!targetRef.trim() || !text.trim()}>
            Save
          </button>
        </div>
      </form>
    </details>
  )
}

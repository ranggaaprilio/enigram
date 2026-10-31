import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import {
  deleteEntity,
  deleteRelation,
  editEntity,
  editRelation,
  getJob,
  mergeEntities,
  messageOf,
  searchGraph,
  type GraphEntity,
  type GraphRelation,
  type GraphSearch,
  type Job,
  type JobAccepted,
} from '../api'

const POLL_MS = 3000

// One row's identity: entities by name, relations by both ends (as in Trace).
const entityKey = (e: GraphEntity) => `e:${e.name}`
const relationKey = (r: GraphRelation) => `r:${r.source}\u0000${r.target}`

interface Change {
  jobId: number
  keys: string[] // the rows it changes; a merge changes every source and the target
  label: string
  status: Job['status']
  error: string | null
}

const isActive = (c: Change) => c.status === 'queued' || c.status === 'running'

export default function Graph() {
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const [draft, setDraft] = useState(q)
  const [found, setFound] = useState<GraphSearch | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [changes, setChanges] = useState<Change[]>([])
  const [editing, setEditing] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [mergeTarget, setMergeTarget] = useState('')

  const search = useCallback(async () => {
    if (!q) {
      setFound(null)
      return
    }
    try {
      setFound(await searchGraph(q))
      setError(null)
    } catch (e) {
      setError(messageOf(e))
    }
  }, [q])

  useEffect(() => {
    setDraft(q)
    setSelected([])
    setMergeTarget('')
    void search()
  }, [q, search])

  // setTimeout after each poll rather than setInterval, so slow responses never overlap. Every
  // poll replaces `changes`, which schedules the next one even when no status moved.
  const pending = changes.filter(isActive).map((c) => c.jobId)
  const pendingKey = pending.join(',')
  useEffect(() => {
    if (!pendingKey) return
    const ids = pendingKey.split(',').map(Number)
    const timer = setTimeout(async () => {
      const jobs = await Promise.all(ids.map((id) => getJob(id).catch(() => null)))
      const settled = jobs.some((j) => j && (j.status === 'done' || j.status === 'failed'))
      setChanges((list) =>
        list.map((c) => {
          const job = jobs.find((j) => j?.id === c.jobId)
          return job ? { ...c, status: job.status, error: job.error } : c
        }),
      )
      if (settled) void search()
    }, POLL_MS)
    return () => clearTimeout(timer)
  }, [pendingKey, changes, search])

  const busy = (key: string) => changes.some((c) => isActive(c) && c.keys.includes(key))

  async function queue(keys: string[], label: string, send: () => Promise<JobAccepted>) {
    try {
      const { job } = await send()
      setChanges((list) => [
        { jobId: job.id, keys, label, status: job.status, error: job.error },
        ...list,
      ])
      setError(null)
    } catch (e) {
      setError(messageOf(e))
    }
    setEditing(null)
    setConfirming(null)
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    setParams(draft.trim() ? { q: draft.trim() } : {})
  }

  function toggle(name: string) {
    const next = selected.includes(name)
      ? selected.filter((n) => n !== name)
      : [...selected, name]
    setSelected(next)
    if (!next.includes(mergeTarget)) setMergeTarget(next[0] ?? '')
  }

  const target = mergeTarget.trim()
  const mergeSources = selected.filter((n) => n !== target)
  const canMerge = target !== '' && mergeSources.length > 0

  async function merge() {
    const keys = [...mergeSources, target].map((n) => `e:${n}`)
    await queue(keys, `Merge ${mergeSources.join(', ')} into ${target}`, () =>
      mergeEntities({ sources: mergeSources, target }),
    )
    setSelected([])
  }

  return (
    <section>
      <h1>Graph</h1>
      <form className="ask-form" onSubmit={submitSearch}>
        <label htmlFor="graph-q">Find entities and relations containing</label>
        <div className="ask-controls">
          <input
            id="graph-q"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="60%"
          />
          <button type="submit" disabled={!draft.trim()}>
            Search
          </button>
        </div>
        <p className="muted small">
          Fix every entity and relation that still states the old fact: answers follow the graph
          only when it no longer contradicts itself.
        </p>
      </form>

      {error && <p className="error-box">{error}</p>}

      {changes.length > 0 && (
        <ul className="uploads" aria-label="Changes">
          {changes.map((c) => (
            <li key={c.jobId}>
              <span className={`status status-${c.status}`}>{c.status}</span> {c.label}
              {c.error && <span className="error-text"> {c.error}</span>}
            </li>
          ))}
        </ul>
      )}

      {found && (
        <>
          <h2>Entities ({found.entities.length})</h2>
          {found.entities.length === 0 ? (
            <p className="muted">No entity mentions “{q}”.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>
                      <span className="visually-hidden">Merge</span>
                    </th>
                    <th>Name</th>
                    <th>Type</th>
                    <th>Description</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {found.entities.map((e) => {
                    const key = entityKey(e)
                    return (
                      <tr key={key}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Select ${e.name}`}
                            checked={selected.includes(e.name)}
                            onChange={() => toggle(e.name)}
                          />
                        </td>
                        <td>{e.name}</td>
                        <td>{e.type}</td>
                        <td>
                          {editing === key ? (
                            <EditForm
                              description={e.description ?? ''}
                              extraLabel="Type"
                              extra={e.type ?? ''}
                              onCancel={() => setEditing(null)}
                              onSave={(description, entity_type) =>
                                queue([key], `Edit ${e.name}`, () =>
                                  editEntity({ name: e.name, description, entity_type }),
                                )
                              }
                            />
                          ) : (
                            e.description
                          )}
                        </td>
                        <td className="nowrap">
                          <RowActions
                            label={e.name}
                            disabled={busy(key)}
                            hidden={editing === key}
                            confirming={confirming === key}
                            onEdit={() => setEditing(key)}
                            onDelete={() => setConfirming(key)}
                            onCancelDelete={() => setConfirming(null)}
                            onConfirmDelete={() =>
                              queue([key], `Delete ${e.name}`, () => deleteEntity(e.name))
                            }
                          />
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          {selected.length > 0 && (
            <div className="ask-controls">
              <label htmlFor="merge-target">Merge selected into</label>
              <input
                id="merge-target"
                value={mergeTarget}
                onChange={(e) => setMergeTarget(e.target.value)}
              />
              <button type="button" disabled={!canMerge} onClick={() => void merge()}>
                Merge
              </button>
            </div>
          )}

          <h2>Relations ({found.relations.length})</h2>
          {found.relations.length === 0 ? (
            <p className="muted">No relation mentions “{q}”.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Source → Target</th>
                    <th>Keywords</th>
                    <th>Description</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {found.relations.map((r) => {
                    const key = relationKey(r)
                    const label = `${r.source} → ${r.target}`
                    return (
                      <tr key={key}>
                        <td>{label}</td>
                        <td>{r.keywords}</td>
                        <td>
                          {editing === key ? (
                            <EditForm
                              description={r.description ?? ''}
                              extraLabel="Keywords"
                              extra={r.keywords ?? ''}
                              onCancel={() => setEditing(null)}
                              onSave={(description, keywords) =>
                                queue([key], `Edit ${label}`, () =>
                                  editRelation({
                                    source: r.source,
                                    target: r.target,
                                    description,
                                    keywords,
                                  }),
                                )
                              }
                            />
                          ) : (
                            r.description
                          )}
                        </td>
                        <td className="nowrap">
                          <RowActions
                            label={label}
                            disabled={busy(key)}
                            hidden={editing === key}
                            confirming={confirming === key}
                            onEdit={() => setEditing(key)}
                            onDelete={() => setConfirming(key)}
                            onCancelDelete={() => setConfirming(null)}
                            onConfirmDelete={() =>
                              queue([key], `Delete ${label}`, () =>
                                deleteRelation(r.source, r.target),
                              )
                            }
                          />
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  )
}

function RowActions(props: {
  label: string
  disabled: boolean
  hidden: boolean
  confirming: boolean
  onEdit: () => void
  onDelete: () => void
  onCancelDelete: () => void
  onConfirmDelete: () => void
}) {
  if (props.hidden) return null
  if (props.confirming) {
    return (
      <>
        Delete {props.label}?{' '}
        <button type="button" onClick={props.onConfirmDelete}>
          Yes
        </button>{' '}
        <button type="button" onClick={props.onCancelDelete}>
          No
        </button>
      </>
    )
  }
  return (
    <>
      <button
        type="button"
        disabled={props.disabled}
        aria-label={`Edit ${props.label}`}
        onClick={props.onEdit}
      >
        Edit
      </button>{' '}
      <button
        type="button"
        disabled={props.disabled}
        aria-label={`Delete ${props.label}`}
        onClick={props.onDelete}
      >
        Delete
      </button>
    </>
  )
}

// Description plus one more field: the entity type, or the relation keywords.
function EditForm(props: {
  description: string
  extraLabel: string
  extra: string
  onSave: (description: string, extra: string) => void
  onCancel: () => void
}) {
  const [description, setDescription] = useState(props.description)
  const [extra, setExtra] = useState(props.extra)
  return (
    <form
      className="ask-form"
      onSubmit={(event) => {
        event.preventDefault()
        props.onSave(description, extra)
      }}
    >
      <label>
        Description
        <textarea rows={5} value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <label>
        {props.extraLabel}
        <input value={extra} onChange={(e) => setExtra(e.target.value)} />
      </label>
      <div className="ask-controls">
        <button type="submit" disabled={!description.trim()}>
          Save
        </button>
        <button type="button" onClick={props.onCancel}>
          Cancel
        </button>
      </div>
    </form>
  )
}

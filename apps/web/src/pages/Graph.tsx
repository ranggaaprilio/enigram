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
import { Merge, Search } from 'lucide-react'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import PageHeader from '@/components/PageHeader'
import StatusBadge from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'

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
    const next = selected.includes(name) ? selected.filter((n) => n !== name) : [...selected, name]
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
      <PageHeader
        title="Graph"
        description="Fix every entity and relation that still states the old fact: answers follow the graph only when it no longer contradicts itself."
      />
      <form className="mb-6 flex flex-col gap-2" onSubmit={submitSearch}>
        <Label htmlFor="graph-q" className="text-muted-foreground">
          Find entities and relations containing
        </Label>
        <div className="flex max-w-xl gap-2">
          <Input
            id="graph-q"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="60%"
          />
          <Button type="submit" disabled={!draft.trim()}>
            <Search /> Search
          </Button>
        </div>
      </form>

      {error && <ErrorAlert>{error}</ErrorAlert>}

      {changes.length > 0 && (
        <ul className="my-4 flex flex-col gap-2" aria-label="Changes">
          {changes.map((c) => (
            <li
              key={c.jobId}
              className="flex flex-wrap items-center gap-2 rounded-md bg-card px-3.5 py-2.5 text-sm"
            >
              <StatusBadge status={c.status} />
              <span>{c.label}</span>
              {c.error && <span className="text-destructive">{c.error}</span>}
            </li>
          ))}
        </ul>
      )}

      {!found && (
        <Empty>Search for a phrase to list the entities and relations that state it.</Empty>
      )}

      {found && (
        <>
          <h2>Entities ({found.entities.length})</h2>
          {found.entities.length === 0 ? (
            <Empty>No entity mentions “{q}”.</Empty>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8">
                    <span className="sr-only">Merge</span>
                  </TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {found.entities.map((e) => {
                  const key = entityKey(e)
                  const isSelected = selected.includes(e.name)
                  return (
                    <TableRow key={key} data-state={isSelected ? 'selected' : undefined}>
                      <TableCell>
                        <Checkbox
                          aria-label={`Select ${e.name}`}
                          checked={isSelected}
                          onCheckedChange={() => toggle(e.name)}
                        />
                      </TableCell>
                      <TableCell className="font-medium">{e.name}</TableCell>
                      <TableCell className="text-muted-foreground">{e.type}</TableCell>
                      <TableCell className="min-w-80">
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
                      </TableCell>
                      <TableCell>
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
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}

          {selected.length > 0 && (
            <div className="my-4 flex flex-wrap items-center gap-3 rounded-xl bg-card px-5 py-4">
              <Label htmlFor="merge-target">Merge selected into</Label>
              <Input
                id="merge-target"
                className="w-64"
                value={mergeTarget}
                onChange={(e) => setMergeTarget(e.target.value)}
              />
              <Button type="button" disabled={!canMerge} onClick={() => void merge()}>
                <Merge /> Merge
              </Button>
            </div>
          )}

          <h2>Relations ({found.relations.length})</h2>
          {found.relations.length === 0 ? (
            <Empty>No relation mentions “{q}”.</Empty>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Source → Target</TableHead>
                  <TableHead>Keywords</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {found.relations.map((r) => {
                  const key = relationKey(r)
                  const label = `${r.source} → ${r.target}`
                  return (
                    <TableRow key={key}>
                      <TableCell className="font-medium">{label}</TableCell>
                      <TableCell className="text-muted-foreground">{r.keywords}</TableCell>
                      <TableCell className="min-w-80">
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
                      </TableCell>
                      <TableCell>
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
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
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
      <div className="flex flex-wrap items-center gap-1.5 text-[13px]">
        <span>Delete {props.label}?</span>
        <Button type="button" size="sm" variant="soft-destructive" onClick={props.onConfirmDelete}>
          Yes
        </Button>
        <Button type="button" size="sm" variant="secondary" onClick={props.onCancelDelete}>
          No
        </Button>
      </div>
    )
  }
  return (
    <div className="flex gap-1.5">
      <Button
        type="button"
        size="sm"
        variant="secondary"
        disabled={props.disabled}
        aria-label={`Edit ${props.label}`}
        onClick={props.onEdit}
      >
        Edit
      </Button>
      <Button
        type="button"
        size="sm"
        variant="soft-destructive"
        disabled={props.disabled}
        aria-label={`Delete ${props.label}`}
        onClick={props.onDelete}
      >
        Delete
      </Button>
    </div>
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
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault()
        props.onSave(description, extra)
      }}
    >
      <Label className="flex-col items-stretch gap-1.5 text-[13px] text-muted-foreground">
        Description
        <Textarea
          rows={5}
          className="text-foreground"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </Label>
      <Label className="flex-col items-stretch gap-1.5 text-[13px] text-muted-foreground">
        {props.extraLabel}
        <Input
          className="text-foreground"
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
        />
      </Label>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={!description.trim()}>
          Save
        </Button>
        <Button type="button" size="sm" variant="secondary" onClick={props.onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { toast } from 'sonner'
import { useSearchParams } from 'react-router'
import { useAccess } from '../access'
import {
  deleteEntity,
  deleteRelation,
  editEntity,
  editRelation,
  entityRelations,
  getJob,
  graphTypes,
  mergeEntities,
  messageOf,
  searchGraph,
  type GraphEntity,
  type GraphRelation,
  type GraphSearch,
  type GraphType,
  type Job,
  type JobAccepted,
} from '../api'
import { Merge, Search, X } from 'lucide-react'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
import PageHeader from '@/components/PageHeader'
import StatusBadge from '@/components/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { WsLink } from '@/workspace'

const POLL_MS = 3000
const PAGE = 50
const TYPING_MS = 300

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

// What the side panel shows: one entity or one relation.
type Panel = { kind: 'entity'; entity: GraphEntity } | { kind: 'relation'; relation: GraphRelation }

type Tab = 'entities' | 'relations'

const isActive = (c: Change) => c.status === 'queued' || c.status === 'running'

function addNew<T>(list: T[], more: T[], key: (item: T) => string): T[] {
  const seen = new Set(list.map(key))
  return [...list, ...more.filter((item) => !seen.has(key(item)))]
}

export default function Graph() {
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const type = params.get('type') ?? ''
  const tab: Tab = params.get('tab') === 'relations' ? 'relations' : 'entities'
  const [draft, setDraft] = useState(q)
  const [found, setFound] = useState<GraphSearch | null>(null)
  const [more, setMore] = useState({ entities: false, relations: false })
  const [types, setTypes] = useState<GraphType[]>([])
  const [error, setError] = useState<string | null>(null)
  const [changes, setChanges] = useState<Change[]>([])
  const [panel, setPanel] = useState<Panel | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [mergeTarget, setMergeTarget] = useState('')
  const canEdit = useAccess().can('maintainer') // docs/rbac.md: graph edits

  function update(next: { q?: string; type?: string; tab?: Tab }) {
    const merged = { q, type, tab, ...next }
    const kept: Record<string, string> = {}
    if (merged.q) kept.q = merged.q
    if (merged.type) kept.type = merged.type
    if (merged.tab === 'relations') kept.tab = 'relations'
    setParams(kept, { replace: true })
  }

  // No phrase: the whole graph, most connected entities first.
  const search = useCallback(async () => {
    try {
      const page = await searchGraph({ q, type, limit: PAGE })
      setFound(page)
      setMore({
        entities: page.entities.length === PAGE,
        relations: page.relations.length === PAGE,
      })
      setError(null)
    } catch (e) {
      setError(messageOf(e))
    }
  }, [q, type])

  const loadTypes = useCallback(async () => {
    try {
      setTypes(await graphTypes())
    } catch {
      // The filter is a convenience: the list still works without it.
    }
  }, [])

  useEffect(() => {
    setDraft(q)
    setSelected([])
    setMergeTarget('')
    void search()
  }, [q, search])

  useEffect(() => {
    void loadTypes()
  }, [loadTypes])

  // Search as you type: the phrase reaches the URL once typing stops.
  useEffect(() => {
    if (draft.trim() === q) return
    const timer = setTimeout(() => update({ q: draft.trim() }), TYPING_MS)
    return () => clearTimeout(timer)
    // `update` is rebuilt every render from q, type and tab: those are the real inputs.
  }, [draft, q, type, tab])

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
      if (settled) {
        void search()
        void loadTypes()
      }
    }, POLL_MS)
    return () => clearTimeout(timer)
  }, [pendingKey, changes, search, loadTypes])

  const busy = (key: string) => changes.some((c) => isActive(c) && c.keys.includes(key))

  async function queue(keys: string[], label: string, send: () => Promise<JobAccepted>) {
    try {
      const { job } = await send()
      setChanges((list) => [
        { jobId: job.id, keys, label, status: job.status, error: job.error },
        ...list,
      ])
      setError(null)
      toast.success(`Queued: ${label}`)
      return true
    } catch (e) {
      setError(messageOf(e))
      return false
    }
  }

  async function loadMore() {
    if (!found) return
    try {
      if (tab === 'entities') {
        const page = await searchGraph({ q, type, limit: PAGE, offset: found.entities.length })
        setFound({ ...found, entities: addNew(found.entities, page.entities, entityKey) })
        setMore({ ...more, entities: page.entities.length === PAGE })
      } else {
        const page = await searchGraph({ q, type, limit: PAGE, offset: found.relations.length })
        setFound({ ...found, relations: addNew(found.relations, page.relations, relationKey) })
        setMore({ ...more, relations: page.relations.length === PAGE })
      }
    } catch (e) {
      setError(messageOf(e))
    }
  }

  // An entity named elsewhere (a relation end) may not be on the loaded page: look it up.
  async function openEntity(name: string) {
    const known = found?.entities.find((e) => e.name === name)
    if (known) return setPanel({ kind: 'entity', entity: known })
    try {
      const hit = (await searchGraph({ q: name, limit: 200 })).entities.find((e) => e.name === name)
      if (hit) setPanel({ kind: 'entity', entity: hit })
      else setError(`No entity ${name}`)
    } catch (e) {
      setError(messageOf(e))
    }
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    update({ q: draft.trim() })
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

  const graphIsEmpty =
    found !== null && !q && !type && found.entities.length === 0 && found.relations.length === 0
  const count = (n: number, hasMore: boolean) => `${n}${hasMore ? '+' : ''}`

  return (
    <section>
      <PageHeader
        title="Graph"
        description="Fix every entity and relation that still states the old fact: answers follow the graph only when it no longer contradicts itself."
      />
      <form className="mb-4 flex flex-wrap items-end gap-2" role="search" onSubmit={submitSearch}>
        <div className="flex min-w-64 flex-1 flex-col gap-1.5 sm:max-w-xl">
          <Label htmlFor="graph-q" className="text-muted-foreground">
            Find entities and relations containing
          </Label>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="graph-q"
              className="pr-9 pl-9"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Name or phrase, e.g. 60%"
            />
            {draft && (
              <button
                type="button"
                aria-label="Clear search"
                className="absolute top-1/2 right-3 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                onClick={() => {
                  setDraft('')
                  update({ q: '' })
                }}
              >
                <X className="size-4" />
              </button>
            )}
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="graph-type" className="text-muted-foreground">
            Entity type
          </Label>
          <NativeSelect
            id="graph-type"
            value={type}
            onChange={(e) => update({ type: e.target.value })}
          >
            <NativeSelectOption value="">All types</NativeSelectOption>
            {types.map((t) => (
              <NativeSelectOption key={t.type} value={t.type}>
                {t.type} ({t.count})
              </NativeSelectOption>
            ))}
          </NativeSelect>
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

      {!found && !error && <Loading />}

      {graphIsEmpty && (
        <Empty
          action={
            <Button asChild variant="secondary" size="sm">
              <WsLink to="/documents">Upload documents</WsLink>
            </Button>
          }
        >
          No graph yet. Upload documents to build it.
        </Empty>
      )}

      {found && !graphIsEmpty && (
        <>
          <div role="tablist" aria-label="Graph rows" className="mb-3 flex gap-1.5">
            {(['entities', 'relations'] as const).map((t) => (
              <Button
                key={t}
                type="button"
                role="tab"
                size="sm"
                aria-selected={tab === t}
                variant={tab === t ? 'default' : 'secondary'}
                onClick={() => update({ tab: t })}
              >
                {t === 'entities'
                  ? `Entities (${count(found.entities.length, more.entities)})`
                  : `Relations (${count(found.relations.length, more.relations)})`}
              </Button>
            ))}
          </div>

          {tab === 'entities' && (
            <>
              {found.entities.length === 0 ? (
                <Empty>{q ? `No entity mentions “${q}”.` : 'No entity of this type.'}</Empty>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-8">
                        <span className="sr-only">Merge</span>
                      </TableHead>
                      <TableHead>Name</TableHead>
                      <TableHead>Type</TableHead>
                      <TableHead className="text-right">Relations</TableHead>
                      <TableHead>Description</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {found.entities.map((e) => {
                      const isSelected = selected.includes(e.name)
                      return (
                        <TableRow
                          key={entityKey(e)}
                          className="cursor-pointer"
                          data-state={isSelected ? 'selected' : undefined}
                          onClick={() => setPanel({ kind: 'entity', entity: e })}
                        >
                          <TableCell onClick={(event) => event.stopPropagation()}>
                            {canEdit && (
                              <Checkbox
                                aria-label={`Select ${e.name}`}
                                checked={isSelected}
                                onCheckedChange={() => toggle(e.name)}
                              />
                            )}
                          </TableCell>
                          <TableCell className="font-medium">
                            <button
                              type="button"
                              className="text-left hover:underline"
                              aria-label={`Open ${e.name}`}
                            >
                              {e.name}
                            </button>
                          </TableCell>
                          <TableCell>
                            {e.type && <Badge variant="secondary">{e.type}</Badge>}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">{e.degree}</TableCell>
                          <TableCell className="min-w-80 whitespace-normal">
                            <span className="line-clamp-2">{e.description}</span>
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              )}

              {selected.length > 0 && (
                <div className="my-4 flex flex-wrap items-center gap-3 rounded-md bg-card px-5 py-4">
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
            </>
          )}

          {tab === 'relations' &&
            (found.relations.length === 0 ? (
              <Empty>{q ? `No relation mentions “${q}”.` : 'No relation yet.'}</Empty>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Source → Target</TableHead>
                    <TableHead>Keywords</TableHead>
                    <TableHead>Description</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {found.relations.map((r) => (
                    <TableRow
                      key={relationKey(r)}
                      className="cursor-pointer"
                      onClick={() => setPanel({ kind: 'relation', relation: r })}
                    >
                      <TableCell className="font-medium">
                        <button
                          type="button"
                          className="text-left hover:underline"
                          aria-label={`Open ${r.source} → ${r.target}`}
                        >
                          {r.source} → {r.target}
                        </button>
                      </TableCell>
                      <TableCell className="text-muted-foreground">{r.keywords}</TableCell>
                      <TableCell className="min-w-80 whitespace-normal">
                        <span className="line-clamp-2">{r.description}</span>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ))}

          {more[tab] && (
            <div className="my-4 flex justify-center">
              <Button type="button" variant="secondary" onClick={() => void loadMore()}>
                Load more
              </Button>
            </div>
          )}
        </>
      )}

      <Sheet open={panel !== null} onOpenChange={(open) => !open && setPanel(null)}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
          {panel?.kind === 'entity' && (
            <EntityPanel
              key={panel.entity.name}
              entity={found?.entities.find((e) => e.name === panel.entity.name) ?? panel.entity}
              busy={busy(entityKey(panel.entity))}
              onOpenEntity={(name) => void openEntity(name)}
              onOpenRelation={(relation) => setPanel({ kind: 'relation', relation })}
              onEdit={(description, entity_type) =>
                queue([entityKey(panel.entity)], `Edit ${panel.entity.name}`, () =>
                  editEntity({ name: panel.entity.name, description, entity_type }),
                )
              }
              onDelete={async () => {
                const name = panel.entity.name
                if (await queue([`e:${name}`], `Delete ${name}`, () => deleteEntity(name)))
                  setPanel(null)
              }}
            />
          )}
          {panel?.kind === 'relation' && (
            <RelationPanel
              key={relationKey(panel.relation)}
              relation={
                found?.relations.find((r) => relationKey(r) === relationKey(panel.relation)) ??
                panel.relation
              }
              busy={busy(relationKey(panel.relation))}
              onOpenEntity={(name) => void openEntity(name)}
              onEdit={(description, keywords) => {
                const { source, target } = panel.relation
                return queue([relationKey(panel.relation)], `Edit ${source} → ${target}`, () =>
                  editRelation({ source, target, description, keywords }),
                )
              }}
              onDelete={async () => {
                const { source, target } = panel.relation
                const done = await queue(
                  [relationKey(panel.relation)],
                  `Delete ${source} → ${target}`,
                  () => deleteRelation(source, target),
                )
                if (done) setPanel(null)
              }}
            />
          )}
        </SheetContent>
      </Sheet>
    </section>
  )
}

function EntityPanel(props: {
  entity: GraphEntity
  busy: boolean
  onOpenEntity: (name: string) => void
  onOpenRelation: (relation: GraphRelation) => void
  onEdit: (description: string, entityType: string) => Promise<boolean>
  onDelete: () => void
}) {
  const { entity } = props
  const [editing, setEditing] = useState(false)
  const [relations, setRelations] = useState<GraphRelation[] | null>(null)

  useEffect(() => {
    entityRelations(entity.name)
      .then(setRelations)
      .catch(() => setRelations([]))
  }, [entity.name])

  return (
    <>
      <SheetHeader className="pr-10">
        <SheetTitle className="text-lg">{entity.name}</SheetTitle>
        <SheetDescription className="flex items-center gap-2">
          {entity.type && <Badge variant="secondary">{entity.type}</Badge>}
          <span>
            {entity.degree} relation{entity.degree === 1 ? '' : 's'}
          </span>
        </SheetDescription>
      </SheetHeader>
      <div className="flex flex-col gap-5 px-4 pb-6">
        {editing ? (
          <EditForm
            description={entity.description ?? ''}
            extraLabel="Type"
            extra={entity.type ?? ''}
            onCancel={() => setEditing(false)}
            onSave={async (description, entityType) => {
              if (await props.onEdit(description, entityType)) setEditing(false)
            }}
          />
        ) : (
          <>
            <p className="m-0 text-sm whitespace-pre-wrap">
              {entity.description || <span className="text-muted-foreground">No description.</span>}
            </p>
            <RowActions
              label={entity.name}
              disabled={props.busy}
              onEdit={() => setEditing(true)}
              onConfirmDelete={props.onDelete}
            />
          </>
        )}

        <div className="flex flex-col gap-2">
          <h3 className="m-0 text-sm font-semibold">Relations</h3>
          {relations === null ? (
            <Loading rows={2} />
          ) : relations.length === 0 ? (
            <p className="m-0 text-sm text-muted-foreground">No relation.</p>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Relations">
              {relations.map((r) => {
                const other = r.source === entity.name ? r.target : r.source
                const arrow = r.source === entity.name ? '→' : '←'
                return (
                  <li key={relationKey(r)} className="rounded-md bg-card px-3 py-2 text-sm">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-muted-foreground">{arrow}</span>
                      <button
                        type="button"
                        className="font-medium hover:underline"
                        onClick={() => props.onOpenEntity(other)}
                      >
                        {other}
                      </button>
                      <Button
                        type="button"
                        size="xs"
                        variant="ghost"
                        className="ml-auto"
                        aria-label={`Open relation ${r.source} → ${r.target}`}
                        onClick={() => props.onOpenRelation(r)}
                      >
                        Details
                      </Button>
                    </div>
                    {r.description && (
                      <p className="m-0 mt-1 line-clamp-2 text-muted-foreground">{r.description}</p>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>
    </>
  )
}

function RelationPanel(props: {
  relation: GraphRelation
  busy: boolean
  onOpenEntity: (name: string) => void
  onEdit: (description: string, keywords: string) => Promise<boolean>
  onDelete: () => void
}) {
  const { relation } = props
  const [editing, setEditing] = useState(false)
  const label = `${relation.source} → ${relation.target}`
  return (
    <>
      <SheetHeader className="pr-10">
        <SheetTitle className="flex flex-wrap items-center gap-1.5 text-lg">
          <button
            type="button"
            className="hover:underline"
            onClick={() => props.onOpenEntity(relation.source)}
          >
            {relation.source}
          </button>
          <span className="text-muted-foreground">→</span>
          <button
            type="button"
            className="hover:underline"
            onClick={() => props.onOpenEntity(relation.target)}
          >
            {relation.target}
          </button>
        </SheetTitle>
        <SheetDescription>{relation.keywords || 'No keywords'}</SheetDescription>
      </SheetHeader>
      <div className="flex flex-col gap-5 px-4 pb-6">
        {editing ? (
          <EditForm
            description={relation.description ?? ''}
            extraLabel="Keywords"
            extra={relation.keywords ?? ''}
            onCancel={() => setEditing(false)}
            onSave={async (description, keywords) => {
              if (await props.onEdit(description, keywords)) setEditing(false)
            }}
          />
        ) : (
          <>
            <p className="m-0 text-sm whitespace-pre-wrap">
              {relation.description || (
                <span className="text-muted-foreground">No description.</span>
              )}
            </p>
            <RowActions
              label={label}
              disabled={props.busy}
              onEdit={() => setEditing(true)}
              onConfirmDelete={props.onDelete}
            />
          </>
        )}
      </div>
    </>
  )
}

function RowActions(props: {
  label: string
  disabled: boolean
  onEdit: () => void
  onConfirmDelete: () => void
}) {
  const canEdit = useAccess().can('maintainer')
  const [confirming, setConfirming] = useState(false)
  if (!canEdit) return null
  if (confirming) {
    return (
      <div className="flex flex-wrap items-center gap-1.5 text-[13px]">
        <span>Delete {props.label}?</span>
        <Button type="button" size="sm" variant="soft-destructive" onClick={props.onConfirmDelete}>
          Yes
        </Button>
        <Button type="button" size="sm" variant="secondary" onClick={() => setConfirming(false)}>
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
        onClick={() => setConfirming(true)}
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
          rows={8}
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

import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import {
  createAnnotation,
  getAnnotation,
  listAnnotations,
  listRepos,
  messageOf,
  NOTE_TYPES,
  patchAnnotation,
  searchSymbols,
  type Annotation,
  type AnnotationEvent,
  type AnnotationPatch,
  type NoteType,
  type RepoRow,
  type SymbolRow,
  type TargetKind,
} from '../api'
import { ChevronRight, Network, Search } from 'lucide-react'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import PageHeader from '@/components/PageHeader'
import StatusBadge from '@/components/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
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
      <PageHeader
        title="Annotations"
        description="Human notes on code symbols, graph entities and documents. Answers cite them, and they ask for review when the code they describe changes."
        action={
          <Button asChild variant="secondary">
            <Link to="/graph" className="hover:no-underline">
              <Network /> Graph editor
            </Link>
          </Button>
        }
      />
      <div className="mb-4 flex items-center gap-3">
        <Label htmlFor="status" className="text-muted-foreground">
          Status
        </Label>
        <NativeSelect
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
            <NativeSelectOption key={s} value={s}>
              {s.replace('_', ' ')}
            </NativeSelectOption>
          ))}
        </NativeSelect>
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
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {notes === null ? (
        <p className="text-muted-foreground">Loading…</p>
      ) : notes.length === 0 ? (
        <Empty>No annotations.</Empty>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Target</TableHead>
              <TableHead>Note</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Owner</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
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
          </TableBody>
        </Table>
      )}
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
  const meta = 'text-[13px] text-muted-foreground'
  return (
    <>
      <TableRow className={props.events ? 'border-b-0' : undefined}>
        <TableCell title={n.target_ref} className="max-w-72">
          <Badge variant="outline" className="mr-1 rounded-[4px] text-muted-foreground">
            {n.target_kind}
          </Badge>{' '}
          {props.target}
        </TableCell>
        <TableCell className="min-w-72">
          <Badge variant="secondary" className="mr-1.5 rounded-[4px]">
            {NOTE_TYPES[n.note_type]}
          </Badge>
          {n.text}
          {n.by && <div className={meta}>by {n.by}</div>}
          {n.via === 'mcp' && <div className={meta}>via MCP by {n.author}</div>}
          {n.question && <div className={meta}>Question: {n.question}</div>}
          {n.memory_id !== null && <div className={meta}>from memory #{n.memory_id}</div>}
        </TableCell>
        <TableCell>
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusBadge status={n.status} />
            {n.reason && <span className={meta}> ({n.reason})</span>}
            {n.conflict && (
              <Badge variant="outline" className="border-destructive text-destructive">
                conflict
              </Badge>
            )}
          </div>
        </TableCell>
        <TableCell className="text-[13px]">
          {n.owner}
          {n.verified_by && (
            <div className="text-muted-foreground">
              verified by {n.verified_by}{' '}
              <span className="whitespace-nowrap">{formatTime(n.verified_at)}</span>
            </div>
          )}
          {n.verify_by && (
            <div className="text-muted-foreground">
              verify by <span className="whitespace-nowrap">{formatTime(n.verify_by)}</span>
            </div>
          )}
        </TableCell>
        <TableCell>
          <div className="flex flex-wrap items-center gap-1.5">
            {n.status !== 'verified' && (
              <Button
                type="button"
                size="sm"
                onClick={() => props.onChange({ status: 'verified' })}
              >
                Verify
              </Button>
            )}
            {n.status !== 'rejected' &&
              (confirming ? (
                <>
                  <span className="text-[13px]">Reject?</span>
                  <Button
                    type="button"
                    size="sm"
                    variant="soft-destructive"
                    onClick={() => props.onChange({ status: 'rejected' })}
                  >
                    Yes
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    onClick={() => setConfirming(false)}
                  >
                    No
                  </Button>
                </>
              ) : (
                <Button
                  type="button"
                  size="sm"
                  variant="soft-destructive"
                  onClick={() => setConfirming(true)}
                >
                  Reject
                </Button>
              ))}
            <Button
              type="button"
              size="sm"
              variant="secondary"
              onClick={() => props.onChange({ conflict: !n.conflict })}
            >
              {n.conflict ? 'Unmark conflict' : 'Conflict'}
            </Button>
            <Button type="button" size="sm" variant="secondary" onClick={props.onHistory}>
              History
            </Button>
          </div>
          {n.status === 'orphaned' && n.candidates.length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <NativeSelect
                size="sm"
                className="max-w-56"
                aria-label={`Relink ${n.target_ref}`}
                value={relinkTo}
                onChange={(e) => setRelinkTo(e.target.value)}
              >
                {n.candidates.map((c) => (
                  <NativeSelectOption key={c.id} value={c.id}>
                    {c.qualified_name} ({c.file})
                  </NativeSelectOption>
                ))}
              </NativeSelect>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                onClick={() => props.onChange({ target_ref: relinkTo })}
              >
                Relink
              </Button>
            </div>
          )}
        </TableCell>
      </TableRow>
      {props.events && (
        <TableRow>
          <TableCell colSpan={5} className="pt-0">
            <ul className="ml-4 list-disc text-[13px] text-muted-foreground">
              {props.events.map((e) => (
                <li key={e.id}>
                  {formatTime(e.at)} · {e.actor} · {e.action}
                  {e.new && ` · ${Object.keys(e.new).join(', ')}`}
                </li>
              ))}
            </ul>
          </TableCell>
        </TableRow>
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
  const [noteType, setNoteType] = useState<NoteType>('technical')
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
        note_type: noteType,
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

  const field = 'flex flex-col gap-1.5'
  const label = 'text-[13px] text-muted-foreground'
  return (
    <Collapsible defaultOpen={Boolean(props.initialRef)} className="group mb-4 rounded-xl bg-card">
      <CollapsibleTrigger className="flex w-full cursor-pointer items-center gap-2 px-5 py-4 text-left font-semibold tracking-[-0.3px]">
        <ChevronRight className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        New annotation
      </CollapsibleTrigger>
      <CollapsibleContent>
        <form className="flex flex-col gap-4 px-5 pb-5" onSubmit={submit}>
          <div className="flex flex-wrap items-end gap-3">
            <div className={field}>
              <Label htmlFor="note-kind" className={label}>
                Kind
              </Label>
              <NativeSelect
                id="note-kind"
                value={kind}
                onChange={(e) => setKind(e.target.value as TargetKind)}
              >
                {KINDS.map((k) => (
                  <NativeSelectOption key={k}>{k}</NativeSelectOption>
                ))}
              </NativeSelect>
            </div>
            {kind === 'symbol' && (
              <>
                <div className={field}>
                  <Label htmlFor="note-repo" className={label}>
                    Repository
                  </Label>
                  <NativeSelect
                    id="note-repo"
                    value={repoId ?? ''}
                    onChange={(e) => setRepo(Number(e.target.value) || null)}
                  >
                    {props.repos.map((r) => (
                      <NativeSelectOption key={r.id} value={r.id}>
                        {r.name}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                </div>
                <div className={field}>
                  <Label htmlFor="note-symbol" className={label}>
                    Symbol
                  </Label>
                  <Input
                    id="note-symbol"
                    className="w-64"
                    value={lookup}
                    placeholder="logout or AuthService.logout"
                    onChange={(e) => setLookup(e.target.value)}
                  />
                </div>
                <Button type="button" variant="secondary" onClick={() => void find()}>
                  <Search /> Find
                </Button>
              </>
            )}
          </div>
          {found.length > 0 && (
            <ul className="flex flex-col gap-1 text-[13px]">
              {found.map((s) => (
                <li key={s.id}>
                  <Button
                    type="button"
                    variant="link"
                    className="h-auto p-0 text-link"
                    onClick={() => setTargetRef(s.id)}
                  >
                    {s.qualified_name}
                  </Button>{' '}
                  <span className="text-muted-foreground">
                    {s.kind} · {s.file}:{s.line_start}-{s.line_end}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <div className={field}>
            <Label htmlFor="note-target" className={label}>
              Target
            </Label>
            <Input
              id="note-target"
              value={targetRef}
              placeholder="symbol id, qualified name, entity or document name"
              onChange={(e) => setTargetRef(e.target.value)}
            />
          </div>
          <div className={field}>
            <Label htmlFor="note-text" className={label}>
              Note
            </Label>
            <Textarea
              id="note-text"
              rows={3}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div className={field}>
              <Label htmlFor="note-type" className={label}>
                Type
              </Label>
              <NativeSelect
                id="note-type"
                value={noteType}
                onChange={(e) => setNoteType(e.target.value as NoteType)}
              >
                {Object.entries(NOTE_TYPES).map(([value, name]) => (
                  <NativeSelectOption key={value} value={value}>
                    {name}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </div>
            <div className={field}>
              <Label htmlFor="note-by" className={label}>
                By
              </Label>
              <Input
                id="note-by"
                className="w-48"
                value={by}
                onChange={(e) => setBy(e.target.value)}
              />
            </div>
            {kind === 'symbol' && (
              <div className={field}>
                <Label htmlFor="note-scope" className={label}>
                  Scope
                </Label>
                <NativeSelect
                  id="note-scope"
                  value={scope}
                  onChange={(e) => setScope(e.target.value as 'deps' | 'body')}
                >
                  <NativeSelectOption value="deps">body and callees</NativeSelectOption>
                  <NativeSelectOption value="body">body only</NativeSelectOption>
                </NativeSelect>
              </div>
            )}
            <div className={field}>
              <Label htmlFor="note-verify" className={label}>
                Verify by
              </Label>
              <Input
                id="note-verify"
                type="date"
                className="w-44"
                value={verifyBy}
                onChange={(e) => setVerifyBy(e.target.value)}
              />
            </div>
            <Button type="submit" disabled={!targetRef.trim() || !text.trim()}>
              Save
            </Button>
          </div>
        </form>
      </CollapsibleContent>
    </Collapsible>
  )
}

import { useCallback, useEffect, useState } from 'react'
import { patchMessage } from '../toasts'
import { toast } from 'sonner'
import { useSearchParams } from 'react-router'
import { WsLink } from '@/workspace'
import { useAccess } from '../access'
import {
  getAnnotation,
  listAnnotations,
  listRepos,
  messageOf,
  NOTE_TYPES,
  deleteAnnotation,
  patchAnnotation,
  type Annotation,
  type AnnotationEvent,
  type AnnotationPatch,
  type RepoRow,
  type Target,
  type TargetKind,
} from '../api'
import { ChevronRight, Network } from 'lucide-react'
import Empty from '@/components/Empty'
import Loading from '@/components/Loading'
import ErrorAlert from '@/components/ErrorAlert'
import AddNoteForm from '@/components/AddNoteForm'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import PageHeader from '@/components/PageHeader'
import StatusBadge from '@/components/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
import { formatTime } from '../format'

const STATUSES = ['all', 'needs_verification', 'orphaned', 'verified', 'proposed', 'rejected']

// Writes are synchronous (no worker job): every action reloads the list.
// `embedded`: the All notes tab of the Knowledge page, which has the header (item 5, A2).
export default function Annotations({ embedded = false }: { embedded?: boolean }) {
  const [params, setParams] = useSearchParams()
  const status = params.get('status') ?? 'all'
  const openId = Number(params.get('id')) || null
  const [notes, setNotes] = useState<Annotation[] | null>(null)
  const [repos, setRepos] = useState<RepoRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [history, setHistory] = useState<Record<number, AnnotationEvent[]>>({})
  const access = useAccess()

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
      toast.success(patchMessage(body))
      await load()
    } catch (e) {
      setError(messageOf(e))
    }
  }

  async function remove(id: number) {
    try {
      await deleteAnnotation(id)
      setError(null)
      toast.success('Note deleted')
      setHistory(({ [id]: _gone, ...rest }) => rest)
      await load()
    } catch (e) {
      setError(messageOf(e))
    }
  }

  const repoName = (id: number | null) => repos.find((r) => r.id === id)?.name ?? `repo ${id}`
  // ?target_kind=&target_ref=&repo= (links from Trace and the Graph): the note is about that.
  const linkedRef = params.get('target_ref')
  const linkedKind = (params.get('target_kind') as TargetKind) || 'symbol'
  const linkedRepo = Number(params.get('repo')) || null
  const linked: Target | null = linkedRef
    ? {
        kind: linkedKind,
        target_kind: linkedKind,
        target_ref: linkedRef,
        repo: linkedKind === 'symbol' ? linkedRepo : null,
        repo_name: linkedKind === 'symbol' && linkedRepo ? repoName(linkedRepo) : null,
        label: linkedRef.split('#').pop() ?? linkedRef,
        detail: linkedKind === 'symbol' ? linkedRef.split('#')[0] : linkedKind,
        summary: null,
      }
    : null

  return (
    <section>
      {!embedded && (
        <PageHeader
          title="Annotations"
          description="Human notes on code symbols, graph entities and documents. Answers cite them, and they ask for review when the code they describe changes."
          action={
            access.can('maintainer') && (
              <Button asChild variant="secondary">
                <WsLink to="/graph" className="hover:no-underline">
                  <Network /> Graph editor
                </WsLink>
              </Button>
            )
          }
        />
      )}
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
      {access.can('contributor') && (
        <Collapsible defaultOpen={linked !== null} className="group mb-4 rounded-md bg-card">
          <CollapsibleTrigger className="flex w-full cursor-pointer items-center gap-2 px-4 py-3 text-left font-semibold">
            <ChevronRight className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
            Add a note
          </CollapsibleTrigger>
          <CollapsibleContent className="px-4 pb-4">
            <AddNoteForm
              // A link's repository name shows once the repositories are loaded.
              key={`${linked?.target_ref}\u0000${repos.length}`}
              title="New note"
              level={2} // right under the page title
              target={linked}
              onSaved={async () => {
                setError(null)
                await load()
              }}
              onError={setError}
            />
          </CollapsibleContent>
        </Collapsible>
      )}
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {notes === null ? (
        <Loading />
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
                onDelete={() => remove(n.id)}
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
  onDelete: () => void
}) {
  const { note: n } = props
  const [confirming, setConfirming] = useState<'reject' | 'delete' | null>(null)
  // docs/rbac.md: maintainers verify, reject, mark conflicts and relink, never on an agent
  // proposal of their own key; the author or a maintainer deletes.
  const access = useAccess()
  const maintainer = access.can('maintainer')
  const ownProposal = n.via === 'mcp' && access.name !== null && n.author === access.name
  const mayVerify = maintainer && !ownProposal
  const mayDelete = maintainer || (access.can('contributor') && n.author === access.name)
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
            {mayVerify && n.status !== 'verified' && (
              <Button
                type="button"
                size="sm"
                onClick={() => props.onChange({ status: 'verified' })}
              >
                Verify
              </Button>
            )}
            {confirming && (
              <>
                <span className="text-[13px]">
                  {confirming === 'reject' ? 'Reject?' : 'Delete for good?'}
                </span>
                <Button
                  type="button"
                  size="sm"
                  variant="soft-destructive"
                  onClick={() => {
                    setConfirming(null)
                    if (confirming === 'reject') props.onChange({ status: 'rejected' })
                    else props.onDelete()
                  }}
                >
                  Yes
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="secondary"
                  onClick={() => setConfirming(null)}
                >
                  No
                </Button>
              </>
            )}
            {mayVerify && n.status !== 'rejected' && !confirming && (
              <Button
                type="button"
                size="sm"
                variant="soft-destructive"
                onClick={() => setConfirming('reject')}
              >
                Reject
              </Button>
            )}
            {maintainer && (
              <Button
                type="button"
                size="sm"
                variant="secondary"
                onClick={() => props.onChange({ conflict: !n.conflict })}
              >
                {n.conflict ? 'Unmark conflict' : 'Conflict'}
              </Button>
            )}
            <Button type="button" size="sm" variant="secondary" onClick={props.onHistory}>
              History
            </Button>
            {mayDelete && !confirming && (
              <Button
                type="button"
                size="sm"
                variant="soft-destructive"
                onClick={() => setConfirming('delete')}
              >
                Delete
              </Button>
            )}
          </div>
          {maintainer && n.status === 'orphaned' && n.candidates.length > 0 && (
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

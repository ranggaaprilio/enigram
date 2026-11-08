import { useCallback, useEffect, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import { useSearchParams } from 'react-router'
import remarkGfm from 'remark-gfm'
import { toast } from 'sonner'
import { useAccess } from '../access'
import {
  getReview,
  messageOf,
  NOTE_TYPES,
  patchAnnotation,
  type AnnotationPatch,
  type ReviewItem,
  type ReviewReason,
} from '../api'
import { formatTime } from '../format'
import { patchMessage } from '../toasts'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
import PageHeader from '@/components/PageHeader'
import RecheckNote from '@/components/RecheckNote'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { cn } from '@/lib/utils'
import { WsLink } from '@/workspace'

const REASONS: { key: ReviewReason; label: string; hint: string }[] = [
  { key: 'proposed', label: 'Proposals', hint: 'A claim waiting for a maintainer' },
  { key: 'needs_verification', label: 'Re-check', hint: 'The code changed since it was written' },
  { key: 'orphaned', label: 'Orphaned', hint: 'Its symbol is gone; relink or reject it' },
  { key: 'conflict', label: 'Conflicts', hint: 'Verified, but it contradicts the code' },
]

const muted = 'text-[13px] text-muted-foreground'

// Plan item 5, A2: the reviewers' queue. Each note shows what it is about now, and every review
// action happens here, without opening the code.
export default function Review() {
  const [items, setItems] = useState<ReviewItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // ?show=<reason> opens one filter (the Dashboard links here).
  const [params] = useSearchParams()
  const asked = REASONS.find((r) => r.key === params.get('show'))?.key
  const [shown, setShown] = useState<ReviewReason | 'all'>(asked ?? 'all')
  // Bulk decisions (A3): ids picked with the checkboxes.
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setItems(await getReview())
    } catch (e) {
      setError(messageOf(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function act(id: number, body: AnnotationPatch) {
    try {
      await patchAnnotation(id, body)
      setError(null)
      toast.success(patchMessage(body))
    } catch (e) {
      setError(messageOf(e))
    }
    await load()
  }

  // One request per note, in order; the API checks each, so a refusal names its note.
  async function bulk(kind: 'verify' | 'reject') {
    const chosen = (items ?? []).filter(
      (i) => picked.has(i.note.id) && (kind === 'reject' || i.reason === 'proposed'),
    )
    setBusy(true)
    const failed: string[] = []
    for (const i of chosen) {
      try {
        await patchAnnotation(i.note.id, { status: kind === 'verify' ? 'verified' : 'rejected' })
      } catch (e) {
        failed.push(`note#${i.note.id}: ${messageOf(e)}`)
      }
    }
    const done = chosen.length - failed.length
    if (done)
      toast.success(`${kind === 'verify' ? 'Verified' : 'Rejected'} ${done} of ${chosen.length}`)
    setError(failed.length ? failed.join('; ') : null)
    setPicked(new Set())
    setBusy(false)
    await load()
  }

  function toggle(id: number) {
    setPicked((now) => {
      const next = new Set(now)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const pickedItems = (items ?? []).filter((i) => picked.has(i.note.id))
  const verifiable = pickedItems.filter((i) => i.reason === 'proposed').length

  const count = (key: ReviewReason) => items?.filter((i) => i.reason === key).length ?? 0
  const visible = items?.filter((i) => shown === 'all' || i.reason === shown) ?? []
  const filters = [{ key: 'all' as const, label: 'All', hint: 'Everything waiting' }, ...REASONS]

  return (
    <section>
      <PageHeader
        title="Inbox"
        description="Notes waiting for a person: proposals to approve, notes the code moved under, notes whose code is gone, and notes that contradict the code."
      />
      <div role="group" aria-label="Show" className="mb-4 flex flex-wrap gap-1.5">
        {filters.map((r) => (
          <Button
            key={r.key}
            type="button"
            size="sm"
            variant={shown === r.key ? 'default' : 'secondary'}
            aria-pressed={shown === r.key}
            title={r.hint}
            onClick={() => setShown(r.key)}
          >
            {r.label}
            {items && (
              <span className="tabular-nums opacity-70">
                {r.key === 'all' ? items.length : count(r.key)}
              </span>
            )}
          </Button>
        ))}
      </div>
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {picked.size > 0 && (
        <div
          role="group"
          aria-label="Selected notes"
          className="sticky top-12 z-[5] mb-3 flex flex-wrap items-center gap-2 rounded-md border bg-background px-3 py-2 shadow-sm"
        >
          <span className="text-sm font-medium">{picked.size} selected</span>
          <Button
            type="button"
            size="sm"
            disabled={busy || verifiable === 0}
            title="Only proposals can be verified in bulk"
            onClick={() => void bulk('verify')}
          >
            Verify {verifiable}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="soft-destructive"
            disabled={busy}
            onClick={() => void bulk('reject')}
          >
            Reject {picked.size}
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setPicked(new Set())}>
            Clear
          </Button>
        </div>
      )}
      {items === null ? (
        !error && <Loading rows={4} />
      ) : visible.length === 0 ? (
        <Empty>Nothing waits for review{shown === 'all' ? '' : ' here'}.</Empty>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-3 p-0">
          {visible.map((item) => (
            <InboxItem
              key={item.note.id}
              item={item}
              picked={picked.has(item.note.id)}
              onPick={() => toggle(item.note.id)}
              onAct={(body) => act(item.note.id, body)}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

function InboxItem({
  item,
  picked,
  onPick,
  onAct,
}: {
  item: ReviewItem
  picked: boolean
  onPick: () => void
  onAct: (body: AnnotationPatch) => Promise<void>
}) {
  const { note, reason } = item
  const access = useAccess()
  const [relinkTo, setRelinkTo] = useState(note.candidates[0]?.id ?? '')
  // docs/rbac.md: maintainers decide, never on an agent proposal of their own key.
  const ownProposal = note.via === 'mcp' && access.name !== null && note.author === access.name
  const mayDecide = access.can('maintainer') && !ownProposal
  const symbol = note.target_kind === 'symbol'
  const name = symbol ? (note.target_ref.split('#').pop() ?? note.target_ref) : note.target_ref
  const where = symbol
    ? `/knowledge?repo=${note.repo_id}&symbol=${encodeURIComponent(note.target_ref)}`
    : null
  const reasonLabel = REASONS.find((r) => r.key === reason)!
  const relink = reason === 'orphaned' && note.candidates.length > 0
  // A re-check needs its own look at then vs now: not decided in bulk.
  const bulkable = mayDecide && reason !== 'needs_verification'

  let context: ReactNode = null
  if (reason === 'needs_verification' && symbol) {
    // Then vs now, with its three outcomes (maintainers only, inside RecheckNote).
    context = (
      <RecheckNote
        note={{ ...note, summary_text: item.summary_then }}
        summary={{
          flow: item.summary_now_kind === 'flow' ? item.summary_now : null,
          symbol: item.summary_now_kind === 'symbol' ? item.summary_now : null,
        }}
        onAct={onAct}
      />
    )
  } else if (item.summary_now) {
    context = (
      <div className="mt-2 rounded-md bg-card px-3 py-2">
        <p className="m-0 mb-0.5 text-[13px] font-semibold text-muted-foreground">
          What the code does now
        </p>
        <div className="markdown text-[13px]">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.summary_now}</ReactMarkdown>
        </div>
      </div>
    )
  }

  const actions: {
    label: string
    body: AnnotationPatch
    variant: 'default' | 'secondary' | 'soft-destructive'
  }[] =
    reason === 'proposed'
      ? [
          { label: 'Verify', body: { status: 'verified' }, variant: 'default' },
          { label: 'Reject', body: { status: 'rejected' }, variant: 'soft-destructive' },
        ]
      : reason === 'orphaned'
        ? [{ label: 'Reject', body: { status: 'rejected' }, variant: 'soft-destructive' }]
        : reason === 'conflict'
          ? [
              { label: 'Code is right after all', body: { conflict: false }, variant: 'secondary' },
              {
                label: 'Note is outdated',
                body: { status: 'rejected' },
                variant: 'soft-destructive',
              },
            ]
          : [] // a re-check decides inside RecheckNote

  return (
    <li className={cn('rounded-md border p-4', picked && 'border-foreground')}>
      <div className="flex flex-wrap items-center gap-2">
        {bulkable && (
          <Checkbox
            aria-label={`Select note ${note.id}`}
            checked={picked}
            onCheckedChange={onPick}
          />
        )}
        <Badge variant="outline" title={reasonLabel.hint}>
          {reasonLabel.label}
        </Badge>
        <Badge variant="secondary">{NOTE_TYPES[note.note_type]}</Badge>
        {where ? (
          <WsLink to={where} className="font-medium [overflow-wrap:anywhere]">
            {name}
          </WsLink>
        ) : (
          <span className="font-medium [overflow-wrap:anywhere]">{name}</span>
        )}
        <span className={muted}>
          {symbol ? (item.repo_name ?? `repo ${note.repo_id}`) : note.target_kind}
        </span>
      </div>
      <p className="mt-2 mb-1">{note.text}</p>
      <p className={cn('m-0', muted)}>
        {note.via === 'mcp'
          ? `Proposed by agent key ${note.author}`
          : `By ${note.by || note.author}`}
        {' · '}
        {formatTime(note.created_at)}
        {note.question && ` · Question: ${note.question}`}
      </p>
      {context}
      {mayDecide && (actions.length > 0 || relink) && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {relink && (
            <>
              <NativeSelect
                size="sm"
                className="max-w-64"
                aria-label={`Relink ${name}`}
                value={relinkTo}
                onChange={(e) => setRelinkTo(e.target.value)}
              >
                {note.candidates.map((c) => (
                  <NativeSelectOption key={c.id} value={c.id}>
                    {c.qualified_name} ({c.file})
                  </NativeSelectOption>
                ))}
              </NativeSelect>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                onClick={() => void onAct({ target_ref: relinkTo })}
              >
                Relink
              </Button>
            </>
          )}
          {actions.map((a) => (
            <Button
              key={a.label}
              type="button"
              size="sm"
              variant={a.variant}
              onClick={() => void onAct(a.body)}
            >
              {a.label}
            </Button>
          ))}
        </div>
      )}
      {!mayDecide && reason !== 'needs_verification' && (
        <p className={cn('mt-3 mb-0', muted)}>
          {ownProposal
            ? 'Your own agent proposal: another maintainer decides.'
            : 'A maintainer decides.'}
        </p>
      )}
    </li>
  )
}

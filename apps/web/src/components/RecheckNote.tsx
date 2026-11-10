import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Annotation, AnnotationPatch, SymbolView } from '../api'
import { Button } from '@/components/ui/button'
import { useAccess } from '../access'

const REASONS: Record<string, string> = {
  hash: 'The code of this symbol changed.',
  deps: 'Code it calls changed.',
  orphan: 'The note was moved, or its symbol was removed and came back.',
  verify_by: 'Its review date passed.',
}

const ACTIONS: {
  label: string
  body: AnnotationPatch
  variant: 'secondary' | 'soft-destructive'
}[] = [
  { label: 'Still right', body: { status: 'verified', conflict: false }, variant: 'secondary' },
  {
    label: 'Code is wrong now',
    body: { status: 'verified', conflict: true },
    variant: 'soft-destructive',
  },
  { label: 'Note is outdated', body: { status: 'rejected' }, variant: 'secondary' },
]

const label = 'mt-2 mb-0.5 text-[13px] font-semibold text-muted-foreground'
const muted = 'text-[13px] text-muted-foreground'

// A flagged note on the Code page: the summary it was written against next to the summary now,
// so PM/QA can re-check it without reading a code diff. `onAct` reports its own errors.
export default function RecheckNote(props: {
  note: Annotation
  summary: SymbolView['summary']
  onAct: (body: AnnotationPatch) => Promise<void>
}) {
  const { note, summary } = props
  const [busy, setBusy] = useState(false)
  // plans/rbac.md: the three outcomes verify or reject the note, which needs a maintainer.
  const maintainer = useAccess().can('maintainer')
  const then = note.summary_text ?? null
  // As the page and the server: the flow, else the symbol's own. "Still right" keeps this one.
  const nowKind = summary.flow ? 'flow' : 'symbol'
  const now = summary[nowKind]

  async function act(body: AnnotationPatch) {
    setBusy(true)
    try {
      await props.onAct(body)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-2 rounded-md bg-background px-3 py-2">
      <p className="text-[13px]">
        Re-check: {(note.reason && REASONS[note.reason]) ?? 'Flagged for a re-check.'}
      </p>
      <p className={label}>Summary when written{note.summary_kind && ` (${note.summary_kind})`}</p>
      {then ? (
        <div className="markdown text-[13px]">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{then}</ReactMarkdown>
        </div>
      ) : (
        <p className={muted}>No summary was saved with this note.</p>
      )}
      <p className={label}>Summary now{now !== null && ` (${nowKind})`}</p>
      {now === null ? (
        <p className={muted}>No summary yet; it is written at the end of a re-index.</p>
      ) : now === then && nowKind === note.summary_kind ? (
        <p className={muted}>Unchanged since the note was written.</p>
      ) : (
        <div className="markdown text-[13px]">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{now}</ReactMarkdown>
        </div>
      )}
      {!maintainer ? (
        <p className={muted}>A maintainer re-checks this note.</p>
      ) : (
        <div className="mt-2 flex flex-wrap gap-2">
          {ACTIONS.map((a) => (
            <Button
              key={a.label}
              type="button"
              size="sm"
              variant={a.variant}
              disabled={busy}
              onClick={() => act(a.body)}
            >
              {a.label}
            </Button>
          ))}
        </div>
      )}
    </div>
  )
}

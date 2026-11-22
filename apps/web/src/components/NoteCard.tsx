import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Link2, MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import { useAccess } from '../access'
import { NOTE_TYPES, type Annotation, type AnnotationPatch, type SymbolView } from '../api'
import { formatTime } from '../format'
import Markdown from '@/components/Markdown'
import MarkdownEditor from '@/components/MarkdownEditor'
import RecheckNote from '@/components/RecheckNote'
import StatusBadge from '@/components/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'

const muted = 'text-[13px] text-muted-foreground'

// One note on the Code page's Notes tab: its kind and status, the text as markdown, who wrote it
// and when; edit in place, copy a link to it, delete it. `onPatch` and `onDelete` report their
// own errors and resolve true once done.
export default function NoteCard(props: {
  note: Annotation
  summary: SymbolView['summary']
  highlighted?: boolean // opened from a link to this note
  onPatch: (body: AnnotationPatch) => Promise<boolean>
  onDelete: () => Promise<boolean>
  onCopyLink: () => void
}) {
  const { note: n } = props
  const access = useAccess()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(n.text)
  const [saving, setSaving] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const card = useRef<HTMLElement>(null)
  // As the API (application/annotations.py) and the All notes table: the author or a maintainer
  // edits and deletes; an edit by anyone a maintainer could not verify for goes back to review.
  const maintainer = access.can('maintainer')
  const own = access.can('contributor') && n.author === access.name
  const mayEdit = maintainer || own
  const ownProposal = n.via === 'mcp' && access.name !== null && n.author === access.name
  const backToReview = !maintainer || ownProposal

  useEffect(() => {
    if (props.highlighted) card.current?.scrollIntoView?.({ block: 'center' })
  }, [props.highlighted])

  // Set by Edit in the menu: the editor keeps the focus the closing menu would give its button.
  const toEditor = useRef(false)

  function edit() {
    setDraft(n.text)
    setEditing(true)
    toEditor.current = true
  }

  async function save() {
    const text = draft.trim()
    if (saving || !text || text === n.text) return
    setSaving(true)
    try {
      if (await props.onPatch({ text })) setEditing(false)
    } finally {
      setSaving(false)
    }
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()
      void save()
    } else if (event.key === 'Escape' && draft === n.text) {
      // Only an untouched draft: a stray Esc must not throw away a rewrite.
      event.preventDefault()
      setEditing(false)
    }
  }

  return (
    <article
      ref={card}
      id={`note-${n.id}`}
      aria-label={`Note ${n.id}`}
      className={cn(
        'scroll-mt-24 rounded-md border bg-background px-4 py-3 transition-shadow',
        n.status === 'needs_verification' && 'border-l-2 border-l-info',
        props.highlighted && 'ring-[3px] ring-ring/40',
      )}
    >
      <header className="flex items-start gap-2">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
          <Badge variant="secondary" className="rounded-[4px]">
            {NOTE_TYPES[n.note_type]}
          </Badge>
          <StatusBadge status={n.status} />
          {n.conflict && (
            <Badge variant="outline" className="border-destructive text-destructive">
              Conflicts with code
            </Badge>
          )}
        </div>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              className="-mt-1 -mr-2 text-muted-foreground"
              aria-label={`Actions for note ${n.id}`}
            >
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            onCloseAutoFocus={(e) => {
              if (!toEditor.current) return
              toEditor.current = false
              e.preventDefault()
              const box = document.getElementById(`note-edit-${n.id}`) as HTMLTextAreaElement | null
              box?.focus()
              box?.setSelectionRange(box.value.length, box.value.length) // write on at the end
            }}
          >
            {mayEdit && (
              <DropdownMenuItem onSelect={edit} disabled={editing}>
                <Pencil /> Edit
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onSelect={props.onCopyLink}>
              <Link2 /> Copy link
            </DropdownMenuItem>
            {mayEdit && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem variant="destructive" onSelect={() => setConfirming(true)}>
                  <Trash2 /> Delete
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      {editing ? (
        <div className="mt-3 flex flex-col gap-2">
          <MarkdownEditor
            id={`note-edit-${n.id}`}
            value={draft}
            onChange={setDraft}
            onKeyDown={onKeyDown}
            ariaLabel={`Edit note ${n.id}`}
            autoFocus
          />
          {backToReview && (
            <p className={cn('m-0', muted)}>Saving sends it back to a maintainer for review.</p>
          )}
          <div className="flex items-center gap-2">
            <Button
              type="button"
              size="sm"
              disabled={saving || !draft.trim() || draft.trim() === n.text}
              onClick={() => void save()}
            >
              Save changes
            </Button>
            <Button type="button" size="sm" variant="secondary" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <span className={muted}>⌘/Ctrl + Enter</span>
          </div>
        </div>
      ) : (
        <Markdown className="mt-2">{n.text}</Markdown>
      )}

      <footer className={cn('mt-2 flex flex-wrap items-center gap-x-1.5', muted)}>
        {n.by && <span>{n.by}</span>}
        {n.by && <span aria-hidden>·</span>}
        <time dateTime={n.created_at}>{formatTime(n.created_at)}</time>
        {n.owner && n.owner !== n.by && (
          <>
            <span aria-hidden>·</span>
            <span>owner {n.owner}</span>
          </>
        )}
        {n.verify_by && (
          <>
            <span aria-hidden>·</span>
            <span>check again {formatTime(n.verify_by)}</span>
          </>
        )}
      </footer>

      {n.status === 'needs_verification' && (
        <RecheckNote
          note={n}
          summary={props.summary}
          onAct={async (body) => void (await props.onPatch(body))}
        />
      )}

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent showCloseButton={false}>
          <DialogTitle>Delete this note?</DialogTitle>
          <DialogDescription>
            Answers stop citing it, and it cannot be brought back.
          </DialogDescription>
          <div className="max-h-40 overflow-y-auto rounded-md bg-card px-3 py-2">
            <Markdown>{n.text}</Markdown>
          </div>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={saving}
              onClick={async () => {
                setSaving(true)
                // Closed either way: a failure shows in the page's error alert, which the
                // dialog's overlay would hide.
                await props.onDelete()
                setSaving(false)
                setConfirming(false)
              }}
            >
              Delete note
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </article>
  )
}

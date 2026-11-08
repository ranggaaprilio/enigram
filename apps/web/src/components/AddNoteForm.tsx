import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { toast } from 'sonner'
import { ChevronRight, Search } from 'lucide-react'
import { useAccess } from '../access'
import {
  createAnnotation,
  noteFromMemory,
  listAnnotations,
  messageOf,
  NOTE_TYPES,
  searchTargets,
  type Annotation,
  type NoteType,
  type Target,
} from '../api'
import { citeLine, SIMILAR, similarity } from '../notes'
import StatusBadge from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

// "Not right" claims the code should act otherwise; "Add context" (and a note from Annotations)
// may be any kind of note.
export type NoteMode = 'mismatch' | 'context'

const TYPES: Record<NoteMode, NoteType[]> = {
  mismatch: ['expected_behavior', 'known_issue'],
  context: ['business_rule', 'expected_behavior', 'known_issue', 'technical'],
}

// Plain words for each kind of note (plan item 5): what it is for, not the stored name.
const MEANING: Record<NoteType, string> = {
  business_rule: 'How it should be, according to the business',
  expected_behavior: 'What should happen for the user',
  known_issue: 'What is wrong or not done yet',
  technical: 'What a developer should know',
}

const QUESTION: Record<NoteType, string> = {
  business_rule: 'Which rule must this feature follow?',
  expected_behavior: 'What should happen?',
  known_issue: 'What goes wrong, and when?',
  technical: 'What should a developer know?',
}

const MISMATCH_QUESTION: Partial<Record<NoteType, string>> = {
  expected_behavior: 'What should happen instead?',
}

// Claims about how the code behaves wait for a developer's review; business rules and technical
// notes by a maintainer count at once.
const CLAIMS: NoteType[] = ['expected_behavior', 'known_issue']

const RECHECK = [
  { months: 0, label: 'No date' },
  { months: 1, label: 'In 1 month' },
  { months: 3, label: 'In 3 months' },
  { months: 6, label: 'In 6 months' },
]

const GROUPS: { kind: Target['kind']; label: string }[] = [
  { kind: 'feature', label: 'Features' },
  { kind: 'symbol', label: 'Code' },
  { kind: 'entity', label: 'Entities' },
  { kind: 'document', label: 'Documents' },
]

const SEARCH_DELAY_MS = 250

function inMonths(months: number): string {
  const date = new Date()
  date.setMonth(date.getMonth() + months)
  return `${date.toISOString().slice(0, 10)}T00:00:00Z`
}

// The one note form (plan item 5, A1): pick what the note is about by searching what you know
// (a route, a name, an entity, a document), pick the kind of note, answer its question.
export default function AddNoteForm(props: {
  title: string
  level?: 2 | 3 // the title's heading level under the page's own headings; 3 by default
  // A note made from an agent memory (Memory page, plan item 7): its text to start from, saved
  // through POST /memories/{id}/note, always as a proposal.
  memory?: { id: number; text: string }
  target?: Target | null // set: the note is about this (the Code page's symbol, a link)
  fixedTarget?: boolean // the target cannot be changed (the symbol on screen)
  mode?: NoteMode
  by?: string // a name kept by the page; without it the form asks (unless signed in)
  onSaved: () => Promise<void>
  onCancel?: () => void
  onError: (message: string) => void
}) {
  const mode = props.mode ?? 'context'
  const access = useAccess()
  const [target, setTarget] = useState<Target | null>(props.target ?? null)
  const [query, setQuery] = useState('')
  const [found, setFound] = useState<Target[] | null>(null)
  const [type, setType] = useState<NoteType>(TYPES[mode][0])
  const [text, setText] = useState(props.memory?.text ?? '')
  const [name, setName] = useState('')
  const [owner, setOwner] = useState('')
  const [recheck, setRecheck] = useState(0)
  const [scope, setScope] = useState<'deps' | 'body'>('deps')
  const [saving, setSaving] = useState(false)
  // Notes already on the target (A3): shown before saving, the similar ones flagged.
  const [existing, setExisting] = useState<Annotation[]>([])
  const box = useRef<HTMLTextAreaElement>(null)
  // The page passes a new function each render; the search effect must not rerun for that.
  const onError = useRef(props.onError)
  onError.current = props.onError
  const by = (props.by ?? name).trim()
  const signedAs = access.user ? access.name : by
  const proposed = mode === 'mismatch' || CLAIMS.includes(type)
  const review = !access.can('maintainer') || proposed || props.memory !== undefined

  // Opened from a button under the summary: bring the form into view (stacked panels on a phone)
  // and put the cursor in it. Switching Not right / Add context keeps the text.
  useEffect(() => {
    setType(TYPES[mode][0])
    if (props.fixedTarget) {
      box.current?.scrollIntoView?.({ block: 'center' })
      box.current?.focus({ preventScroll: true })
    }
  }, [mode, props.fixedTarget])

  useEffect(() => {
    setExisting([])
    if (!target) return
    let current = true
    listAnnotations({
      target_kind: target.target_kind,
      target_ref: target.target_ref,
      repo: target.repo ?? undefined,
    })
      .then((notes) => current && setExisting(notes))
      .catch(() => undefined) // only a hint: the save itself still reports errors
    return () => {
      current = false
    }
  }, [target])

  useEffect(() => {
    const q = query.trim()
    if (!q) {
      setFound(null)
      return
    }
    let current = true
    const timer = setTimeout(() => {
      searchTargets(q)
        .then((rows) => current && setFound(rows))
        .catch((e) => current && onError.current(messageOf(e)))
    }, SEARCH_DELAY_MS)
    return () => {
      current = false
      clearTimeout(timer)
    }
  }, [query])

  async function save() {
    if (saving || !target || !text.trim()) return // a second save would add a duplicate
    setSaving(true)
    try {
      const where = {
        target_kind: target.target_kind,
        target_ref: target.target_ref,
        text: text.trim(),
        note_type: type,
        ...(target.target_kind === 'symbol' && target.repo !== null
          ? { repo: target.repo, scope }
          : {}),
        ...(by && !access.user ? { by } : {}),
      }
      if (props.memory) {
        await noteFromMemory(props.memory.id, where)
        toast.success('Proposal saved for review')
        await props.onSaved()
        return
      }
      await createAnnotation({
        target_kind: target.target_kind,
        target_ref: target.target_ref,
        text: text.trim(),
        note_type: type,
        ...(target.target_kind === 'symbol' && target.repo !== null
          ? { repo: target.repo, scope }
          : {}),
        ...(by && !access.user ? { by } : {}),
        ...(owner.trim() ? { owner: owner.trim() } : {}),
        ...(recheck ? { verify_by: inMonths(recheck) } : {}),
        ...(proposed ? { proposed: true } : {}),
      })
      setText('')
      toast.success(review ? 'Proposal saved for review' : 'Note saved')
      if (!props.fixedTarget) {
        setTarget(props.target ?? null)
        setQuery('') // ready for the next note
      }
      await props.onSaved()
    } catch (e) {
      props.onError(messageOf(e))
    } finally {
      setSaving(false)
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    void save()
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()
      void save()
    }
  }

  const label = 'text-[13px] font-medium text-muted-foreground'
  const question = (mode === 'mismatch' && MISMATCH_QUESTION[type]) || QUESTION[type]
  return (
    <form
      aria-label={props.title}
      className="mt-3 flex flex-col gap-4 rounded-md border bg-background p-4"
      onSubmit={submit}
    >
      {props.level === 2 ? (
        <h2 className="m-0 text-sm font-semibold">{props.title}</h2>
      ) : (
        <h3 className="m-0 text-sm font-semibold">{props.title}</h3>
      )}

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="note-about" className={label}>
          About what?
        </Label>
        {target ? (
          <div className="flex items-start gap-3 rounded-md bg-card px-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="font-medium [overflow-wrap:anywhere]">{target.label}</div>
              <div className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]">
                {[target.repo_name, target.detail].filter(Boolean).join(' · ')}
              </div>
              {target.summary && <p className="mt-1 mb-0 text-[13px]">{target.summary}</p>}
            </div>
            {!props.fixedTarget && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => {
                  setTarget(null)
                  setQuery('')
                }}
              >
                Change
              </Button>
            )}
          </div>
        ) : (
          <>
            <div className="relative">
              <Search
                className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
              <Input
                id="note-about"
                className="pl-9"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="A route, a name, an entity or a document"
                autoComplete="off"
              />
            </div>
            {found !== null &&
              (found.length === 0 ? (
                <p className="m-0 text-[13px] text-muted-foreground">Nothing matches “{query}”.</p>
              ) : (
                <div className="flex flex-col gap-2">
                  {GROUPS.map(({ kind, label: heading }) => {
                    const rows = found.filter((t) => t.kind === kind)
                    if (rows.length === 0) return null
                    return (
                      <div key={kind} role="group" aria-label={heading}>
                        <div className="mb-1 text-xs font-medium text-muted-foreground">
                          {heading}
                        </div>
                        <ul className="m-0 flex list-none flex-col gap-1 p-0">
                          {rows.map((t) => (
                            <li key={`${t.repo}\u0000${t.target_ref}`}>
                              <button
                                type="button"
                                className="w-full cursor-pointer rounded-md px-3 py-1.5 text-left hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
                                onClick={() => setTarget(t)}
                              >
                                <span className="block font-medium [overflow-wrap:anywhere]">
                                  {t.label}
                                </span>
                                <span className="block text-[13px] text-muted-foreground [overflow-wrap:anywhere]">
                                  {[t.repo_name, t.summary ?? t.detail].filter(Boolean).join(' · ')}
                                </span>
                              </button>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )
                  })}
                </div>
              ))}
          </>
        )}
      </div>

      <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0">
        <legend className={cn(label, 'mb-1.5')}>What kind of note?</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {TYPES[mode].map((t) => (
            <label
              key={t}
              className={cn(
                'flex cursor-pointer flex-col gap-0.5 rounded-md border px-3 py-2 has-[input:focus-visible]:ring-[3px] has-[input:focus-visible]:ring-ring/50',
                type === t && 'border-foreground bg-accent',
              )}
            >
              <input
                type="radio"
                name="note-type"
                value={t}
                className="sr-only"
                checked={type === t}
                onChange={() => setType(t)}
              />
              <span className="font-medium">{NOTE_TYPES[t]}</span>
              <span className="text-[13px] text-muted-foreground">{MEANING[t]}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="note-text" className={label}>
          {question}
        </Label>
        <Textarea
          id="note-text"
          ref={box}
          rows={4}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
        />
      </div>

      {existing.length > 0 && (
        <div className="flex flex-col gap-1.5" aria-label="Notes already here">
          <p className={cn(label, 'm-0')}>
            Already noted here ({existing.length}): check yours is new
          </p>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {existing.slice(0, 5).map((n) => {
              const close = text.trim() !== '' && similarity(text, n.text) >= SIMILAR
              return (
                <li
                  key={n.id}
                  className={cn(
                    'rounded-md bg-card px-3 py-1.5 text-[13px]',
                    close && 'bg-warning-bg text-warning',
                  )}
                >
                  {close && <strong className="mr-1.5">Looks the same:</strong>}
                  note#{n.id} {n.text} <StatusBadge status={n.status} />
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {text.trim() && (
        <div className="flex flex-col gap-1" aria-label="How answers cite it">
          <p className={cn(label, 'm-0')}>How answers will cite it</p>
          <p className="m-0 rounded-md bg-card px-3 py-2 font-mono text-[12px] [overflow-wrap:anywhere]">
            <span className="badge badge-human mr-1.5 ml-0 font-sans">human</span>
            {citeLine(text, review ? null : access.name || 'you', new Date())}
          </p>
          {review && (
            <p className="m-0 text-[13px] text-muted-foreground">
              Answers do not cite it until a maintainer verifies it.
            </p>
          )}
        </div>
      )}

      {!access.user && props.by === undefined && (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="note-by" className={label}>
            Your name
          </Label>
          <Input
            id="note-by"
            className="w-56"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
      )}

      {/* Owner and re-check date belong to a note written here, not to a memory's proposal. */}
      {!props.memory && (
        <Collapsible className="group/options">
          <CollapsibleTrigger className="flex cursor-pointer items-center gap-1.5 text-[13px] font-medium text-muted-foreground">
            <ChevronRight className="size-3.5 transition-transform group-data-[state=open]/options:rotate-90" />
            More options
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-3 flex flex-col gap-3">
            <div className="flex flex-wrap gap-4">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="note-owner" className={label}>
                  Owner
                </Label>
                <Input
                  id="note-owner"
                  className="w-48"
                  value={owner}
                  onChange={(e) => setOwner(e.target.value)}
                  placeholder="who keeps it right"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="note-recheck" className={label}>
                  Check it again
                </Label>
                <NativeSelect
                  id="note-recheck"
                  value={recheck}
                  onChange={(e) => setRecheck(Number(e.target.value))}
                >
                  {RECHECK.map((r) => (
                    <NativeSelectOption key={r.months} value={r.months}>
                      {r.label}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              </div>
            </div>
            {target?.target_kind === 'symbol' && (
              <fieldset className="m-0 flex flex-col gap-1 border-0 p-0">
                <legend className={cn(label, 'mb-1')}>Ask for a re-check when</legend>
                {(
                  [
                    ['deps', 'this code or code it calls changes'],
                    ['body', 'only this code changes'],
                  ] as const
                ).map(([value, words]) => (
                  <label key={value} className="flex items-center gap-2 text-sm">
                    <input
                      type="radio"
                      name="note-scope"
                      value={value}
                      checked={scope === value}
                      onChange={() => setScope(value)}
                    />
                    {words}
                  </label>
                ))}
              </fieldset>
            )}
          </CollapsibleContent>
        </Collapsible>
      )}

      <p className="m-0 text-[13px] text-muted-foreground">
        {review
          ? 'Saved as a proposal: a maintainer reviews it before answers cite it.'
          : 'Saved as verified: answers cite it at once.'}
        {signedAs && ` Signed as ${signedAs}.`}
      </p>
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={!target || !text.trim() || saving}>
          Save
        </Button>
        {props.onCancel && (
          <Button type="button" variant="secondary" onClick={props.onCancel}>
            Cancel
          </Button>
        )}
        <span className="text-[13px] text-muted-foreground">⌘/Ctrl + Enter</span>
      </div>
    </form>
  )
}

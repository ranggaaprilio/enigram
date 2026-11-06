import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createAnnotation, messageOf, NOTE_TYPES, type NoteType } from '../api'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { Textarea } from '@/components/ui/textarea'

export type GuidedMode = 'mismatch' | 'context'

// "Not right" claims the code should act otherwise; "Add context" may be any kind of note.
const TYPES: Record<GuidedMode, NoteType[]> = {
  mismatch: ['expected_behavior', 'known_issue'],
  context: ['business_rule', 'expected_behavior', 'known_issue', 'technical'],
}

const PROMPTS: Record<GuidedMode, Partial<Record<NoteType, string>>> = {
  mismatch: {
    expected_behavior: 'What should happen instead?',
    known_issue: 'What goes wrong, and when?',
  },
  context: {
    business_rule: 'Which rule must this feature follow?',
    expected_behavior: 'What should happen?',
    known_issue: 'What goes wrong, and when?',
    technical: 'What should a developer know?',
  },
}

// Claims about how the code behaves wait for a developer; business rules and technical notes
// keep the usual rule (verified when the key owns the note).
const CLAIMS: NoteType[] = ['expected_behavior', 'known_issue']

// The Code page's note form: the target is the symbol on screen, the type picks the question.
export default function GuidedNoteForm(props: {
  repoId: number
  symbolId: string
  mode: GuidedMode
  by: string // the page's "Your name"
  onSaved: () => Promise<void>
  onCancel: () => void
  onError: (message: string) => void
}) {
  const [type, setType] = useState<NoteType>(TYPES[props.mode][0])
  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)
  const proposed = props.mode === 'mismatch' || CLAIMS.includes(type)
  const box = useRef<HTMLTextAreaElement>(null)

  // Opened from a button under the summary: bring the form into view (stacked panels on a
  // phone) and put the cursor in it.
  // Switching between Not right and Add context keeps the text, and picks that mode's type.
  useEffect(() => {
    setType(TYPES[props.mode][0])
    box.current?.scrollIntoView?.({ block: 'center' })
    box.current?.focus({ preventScroll: true })
  }, [props.mode])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (saving) return // a second click while the first save runs would add a duplicate
    setSaving(true)
    try {
      await createAnnotation({
        target_kind: 'symbol',
        target_ref: props.symbolId,
        repo: props.repoId,
        text: text.trim(),
        note_type: type,
        ...(props.by.trim() ? { by: props.by.trim() } : {}),
        ...(proposed ? { proposed: true } : {}),
      })
      await props.onSaved()
    } catch (e) {
      props.onError(messageOf(e))
    } finally {
      setSaving(false)
    }
  }

  const field = 'flex flex-col gap-1.5'
  const label = 'text-[13px] text-muted-foreground'
  return (
    <form className="mt-3 flex flex-col gap-3 rounded-lg bg-background p-4" onSubmit={submit}>
      <h4 className="text-sm font-semibold">
        {props.mode === 'mismatch' ? 'Not right' : 'Add context'}
      </h4>
      <div className={field}>
        <Label htmlFor="guided-type" className={label}>
          Type
        </Label>
        <NativeSelect
          id="guided-type"
          value={type}
          onChange={(e) => setType(e.target.value as NoteType)}
        >
          {TYPES[props.mode].map((t) => (
            <NativeSelectOption key={t} value={t}>
              {NOTE_TYPES[t]}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
      <div className={field}>
        <Label htmlFor="guided-text" className={label}>
          {PROMPTS[props.mode][type]}
        </Label>
        <Textarea
          id="guided-text"
          ref={box}
          rows={4}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <p className="text-[13px] text-muted-foreground">
          {proposed
            ? 'Saved as a proposal; review it on Annotations.'
            : 'Saved as verified.'}
          {props.by.trim() && ` Signed as ${props.by.trim()}.`}
        </p>
      </div>
      <div className="flex gap-2">
        <Button type="submit" disabled={!text.trim() || saving}>
          Save
        </Button>
        <Button type="button" variant="secondary" onClick={props.onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

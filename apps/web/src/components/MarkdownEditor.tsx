import { useCallback, useLayoutEffect, useRef, useState, type KeyboardEvent, type Ref } from 'react'
import { Bold, Code, Heading, Italic, Link, List, ListOrdered, type LucideIcon } from 'lucide-react'
import { applyFormat, type Edit, type Format } from '@/lib/markdown-format'
import Markdown from '@/components/Markdown'
import { cn } from '@/lib/utils'

// The API's limit on a note (ANNOTATION_MAX_CHARS).
export const NOTE_MAX_CHARS = 4000

const MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

const TOOLS: { kind: Format; label: string; icon: LucideIcon; key?: string }[] = [
  { kind: 'bold', label: 'Bold', icon: Bold, key: 'b' },
  { kind: 'italic', label: 'Italic', icon: Italic, key: 'i' },
  { kind: 'heading', label: 'Heading', icon: Heading },
  { kind: 'ul', label: 'Bulleted list', icon: List },
  { kind: 'ol', label: 'Numbered list', icon: ListOrdered },
  { kind: 'code', label: 'Code', icon: Code, key: 'e' },
  { kind: 'link', label: 'Link', icon: Link, key: 'k' },
]

// A note's text: a plain textarea with a formatting toolbar and a preview of how the note will
// read. The textarea stays mounted in Preview, so its label, focus and draft survive the switch.
export default function MarkdownEditor(props: {
  id: string
  value: string
  onChange: (value: string) => void
  onKeyDown?: (event: KeyboardEvent<HTMLTextAreaElement>) => void
  textareaRef?: Ref<HTMLTextAreaElement>
  rows?: number
  placeholder?: string
  autoFocus?: boolean
  ariaLabel?: string // when no <label> points at `id`
}) {
  const [preview, setPreview] = useState(false)
  const box = useRef<HTMLTextAreaElement | null>(null)
  // The selection to restore once the formatted text is rendered.
  const pending = useRef<Edit | null>(null)
  const count = props.value.length

  useLayoutEffect(() => {
    const edit = pending.current
    if (!edit || !box.current) return
    pending.current = null
    box.current.focus()
    box.current.setSelectionRange(edit.start, edit.end)
  }, [props.value])

  // Stable, so React does not detach and reattach the ref on every render.
  const { textareaRef } = props
  const setRef = useCallback(
    (el: HTMLTextAreaElement | null) => {
      box.current = el
      if (typeof textareaRef === 'function') textareaRef(el)
      else if (textareaRef) textareaRef.current = el
    },
    [textareaRef],
  )

  function format(kind: Format) {
    const el = box.current
    if (!el) return
    const edit = applyFormat(props.value, el.selectionStart, el.selectionEnd, kind)
    if (edit.value.length > NOTE_MAX_CHARS) return
    pending.current = edit
    props.onChange(edit.value)
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // ⌘ on a Mac, where Ctrl+B/E/K move the cursor in a text field; Ctrl elsewhere.
    const mod = MAC ? event.metaKey : event.ctrlKey
    const key = event.key.toLowerCase() // Caps Lock gives 'B'
    const tool = mod && !event.altKey && TOOLS.find((t) => t.key === key)
    if (tool) {
      event.preventDefault()
      format(tool.kind)
      return
    }
    props.onKeyDown?.(event)
  }

  const tab = (on: boolean) =>
    cn(
      'cursor-pointer rounded-sm px-2.5 py-1 text-[13px] font-medium text-muted-foreground hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none',
      on && 'bg-background text-foreground shadow-xs',
    )

  return (
    <div className="rounded-md border border-input bg-background focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/25">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-card px-1.5 py-1.5">
        <div className="flex gap-0.5 rounded-md bg-secondary p-0.5">
          <button
            type="button"
            className={tab(!preview)}
            aria-pressed={!preview}
            onClick={() => setPreview(false)}
          >
            Write
          </button>
          <button
            type="button"
            className={tab(preview)}
            aria-pressed={preview}
            onClick={() => setPreview(true)}
          >
            Preview
          </button>
        </div>
        <div role="toolbar" aria-label="Formatting" className="flex flex-wrap gap-0.5">
          {TOOLS.map((t) => (
            <button
              key={t.kind}
              type="button"
              aria-label={t.label}
              title={t.key ? `${t.label} (⌘/Ctrl+${t.key.toUpperCase()})` : t.label}
              disabled={preview}
              className="inline-flex size-7 cursor-pointer items-center justify-center rounded-sm text-muted-foreground hover:bg-secondary hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-40"
              // Keep the selection in the textarea: a click would move focus to the button first.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => format(t.kind)}
            >
              <t.icon className="size-4" />
            </button>
          ))}
        </div>
      </div>
      <textarea
        id={props.id}
        ref={setRef}
        hidden={preview}
        rows={props.rows ?? 5}
        maxLength={NOTE_MAX_CHARS}
        value={props.value}
        placeholder={props.placeholder}
        autoFocus={props.autoFocus}
        aria-label={props.ariaLabel}
        onChange={(e) => props.onChange(e.target.value)}
        onKeyDown={onKeyDown}
        className="block field-sizing-content max-h-[60vh] min-h-28 w-full resize-y bg-transparent px-3 py-2.5 text-sm leading-relaxed outline-none placeholder:text-muted-foreground"
      />
      {preview && (
        <div className="min-h-28 px-3 py-2.5" role="region" aria-label="Preview">
          {props.value.trim() ? (
            <Markdown>{props.value}</Markdown>
          ) : (
            <p className="m-0 text-[13px] text-muted-foreground">Nothing to preview yet.</p>
          )}
        </div>
      )}
      <div className="flex items-center justify-between gap-2 border-t px-3 py-1.5 text-xs text-muted-foreground">
        <span>Markdown: **bold**, _italic_, - lists, `code`</span>
        <span
          className={cn(
            'tabular-nums',
            count > NOTE_MAX_CHARS * 0.9 && 'text-warning',
            count >= NOTE_MAX_CHARS && 'text-destructive',
          )}
        >
          {count} / {NOTE_MAX_CHARS}
        </span>
      </div>
    </div>
  )
}

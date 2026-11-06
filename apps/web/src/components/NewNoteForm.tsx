import { useState, type FormEvent } from 'react'
import { ChevronRight, Search } from 'lucide-react'
import {
  createAnnotation,
  messageOf,
  NOTE_TYPES,
  searchSymbols,
  type NoteType,
  type RepoRow,
  type SymbolRow,
  type TargetKind,
} from '../api'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { Textarea } from '@/components/ui/textarea'

const KINDS: TargetKind[] = ['symbol', 'entity', 'document']

export default function NewNoteForm(props: {
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

import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { useAccess } from '../access'
import {
  deleteMemory,
  getMemory,
  listMemories,
  messageOf,
  NOTE_TYPES,
  type MemoryDetail,
  type MemoryMode,
  type MemoryRow,
} from '../api'
import { formatTime } from '../format'
import AddNoteForm from '@/components/AddNoteForm'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
import PageHeader from '@/components/PageHeader'
import StatusBadge from '@/components/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { WsLink } from '@/workspace'

const SEARCH_DELAY_MS = 300
const MODES: { key: MemoryMode; label: string; hint: string }[] = [
  { key: 'text', label: 'Words', hint: 'Memories that contain these words' },
  { key: 'semantic', label: 'Meaning', hint: 'What search_memory would find, with its score' },
]
const muted = 'text-[13px] text-muted-foreground'

// Plan item 7, M2: what agents remember (MCP add_memory), for people to read, search, delete or
// turn into a note. A maintainer sees every key's memories; anyone else only their own.
export default function Memory() {
  const access = useAccess()
  const all = access.can('maintainer')
  const [params, setParams] = useSearchParams()
  const mode: MemoryMode = params.get('mode') === 'semantic' ? 'semantic' : 'text'
  const key = all ? (params.get('key') ?? '') : ''
  const [q, setQ] = useState(params.get('q') ?? '')
  const [rows, setRows] = useState<MemoryRow[] | null>(null)
  const [keys, setKeys] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)

  function set(name: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value)
    else next.delete(name)
    setParams(next, { replace: true })
  }

  const needle = q.trim()
  const waiting = mode === 'semantic' && !needle // a search by meaning needs words
  useEffect(() => {
    if (waiting) {
      setRows([])
      return
    }
    let current = true
    const timer = setTimeout(
      () => {
        listMemories({ q: needle, mode, key })
          .then((found) => {
            if (!current) return
            setRows(found)
            setError(null)
            // The key filter offers the keys seen without one.
            if (!key && !needle)
              setKeys([...new Set(found.map((m) => m.api_key ?? ''))].filter(Boolean).sort())
          })
          .catch((e) => current && setError(messageOf(e)))
      },
      needle ? SEARCH_DELAY_MS : 0,
    )
    return () => {
      current = false
      clearTimeout(timer)
    }
  }, [needle, mode, key, waiting, reload])

  if (!access.can('contributor'))
    return (
      <section>
        <PageHeader title="Memory" />
        <Empty>Agent memories need the contributor role in this workspace.</Empty>
      </section>
    )

  const filtered = needle || key
  return (
    <section>
      <PageHeader
        title="Memory"
        description={
          all
            ? 'What agents saved with add_memory, every key in this workspace: read it, search it as an agent would, delete what is wrong, or make a note of it.'
            : 'What your key saved with add_memory: read it, search it as your agent would, delete what is wrong, or make a note of it.'
        }
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Input
          type="search"
          aria-label="Search memories"
          placeholder={mode === 'semantic' ? 'What would an agent ask?' : 'Words in a memory'}
          className="max-w-sm min-w-48 flex-1"
          value={q}
          onChange={(e) => {
            setQ(e.target.value)
            set('q', e.target.value.trim())
          }}
        />
        <div role="group" aria-label="Search by" className="flex gap-1">
          {MODES.map((m) => (
            <Button
              key={m.key}
              type="button"
              size="sm"
              variant={mode === m.key ? 'default' : 'secondary'}
              aria-pressed={mode === m.key}
              title={m.hint}
              onClick={() => set('mode', m.key === 'text' ? '' : m.key)}
            >
              {m.label}
            </Button>
          ))}
        </div>
        {all && keys.length > 0 && (
          <NativeSelect
            size="sm"
            aria-label="Key"
            value={key}
            onChange={(e) => set('key', e.target.value)}
          >
            <NativeSelectOption value="">Every key</NativeSelectOption>
            {keys.map((k) => (
              <NativeSelectOption key={k} value={k}>
                {k}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        )}
      </div>
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {waiting ? (
        <p className={muted}>
          Type what an agent would search for: memories are ranked by meaning, as search_memory
          ranks them.
        </p>
      ) : rows === null ? (
        !error && <Loading rows={4} />
      ) : rows.length === 0 ? (
        <Empty>
          {filtered
            ? 'No memory matches.'
            : 'No memories yet. Agents save them with the MCP tool add_memory; they show up here.'}
        </Empty>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-3 p-0">
          {rows.map((m) => (
            <MemoryItem
              key={m.id}
              memory={m}
              showKey={all}
              onGone={() => setReload((n) => n + 1)}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

function MemoryItem(props: { memory: MemoryRow; showKey: boolean; onGone: () => void }) {
  const m = props.memory
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<MemoryDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [noting, setNoting] = useState(false)
  const [confirming, setConfirming] = useState(false)

  async function load() {
    try {
      setDetail(await getMemory(m.id))
      setError(null)
    } catch (e) {
      setError(messageOf(e))
    }
  }

  async function toggle() {
    if (!open && !detail) await load()
    setOpen(!open)
  }

  async function remove() {
    try {
      await deleteMemory(m.id)
      toast.success(`Memory ${m.id} deleted`)
      props.onGone()
    } catch (e) {
      setError(messageOf(e))
      setConfirming(false)
    }
  }

  const textId = `memory-${m.id}-text`
  return (
    <li className="rounded-md border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium tabular-nums">#{m.id}</span>
        {props.showKey && <Badge variant="secondary">{m.api_key ?? 'no key'}</Badge>}
        <span className={muted}>{formatTime(m.created_at)}</span>
        {m.score !== undefined && (
          <Badge variant="outline" title="Similarity to the search: 1 = same meaning">
            score {m.score.toFixed(2)}
          </Badge>
        )}
        {m.stale_model && (
          <span title="A search by meaning (and search_memory) never finds it">
            <StatusBadge status="budget_exceeded">older model</StatusBadge>
          </span>
        )}
      </div>
      <p id={textId} className={open ? 'mt-2 mb-0 whitespace-pre-wrap' : 'mt-2 mb-0 line-clamp-3'}>
        {m.text}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="secondary"
          aria-expanded={open}
          aria-controls={textId}
          onClick={() => void toggle()}
        >
          {open ? 'Less' : 'Open'}
        </Button>
        <Button type="button" size="sm" variant="secondary" onClick={() => setNoting(!noting)}>
          Make a note
        </Button>
        {confirming ? (
          <span className="flex items-center gap-2 rounded-md bg-warning-bg px-3 py-1 text-[13px] text-warning">
            Delete memory #{m.id} for good?
            <Button
              type="button"
              size="sm"
              variant="soft-destructive"
              onClick={() => void remove()}
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
          </span>
        ) : (
          <Button
            type="button"
            size="sm"
            variant="soft-destructive"
            onClick={() => setConfirming(true)}
          >
            Delete
          </Button>
        )}
      </div>
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {open && detail && (
        <div className="mt-3 text-[13px]">
          <p className={`m-0 ${muted}`}>
            Embedded with {detail.embed_model ?? 'an unrecorded model'}
            {detail.stale_model && ': not the current model, so a search by meaning never finds it'}
            .
          </p>
          {detail.notes.length > 0 ? (
            <ul className="mt-2 mb-0 pl-5">
              {detail.notes.map((n) => (
                <li key={n.id}>
                  <WsLink to={`/knowledge?tab=notes&id=${n.id}`} className="underline">
                    Note {n.id}
                  </WsLink>{' '}
                  · {NOTE_TYPES[n.note_type]} · <StatusBadge status={n.status} /> on {n.target_ref}
                </li>
              ))}
            </ul>
          ) : (
            <p className={`mt-1 mb-0 ${muted}`}>No note made from it yet.</p>
          )}
        </div>
      )}
      {noting && (
        <AddNoteForm
          title={`Note from memory #${m.id}`}
          level={2} // the page has no h2 above it
          memory={{ id: m.id, text: m.text }}
          onSaved={async () => {
            setNoting(false)
            await load()
            setOpen(true)
          }}
          onCancel={() => setNoting(false)}
          onError={setError}
        />
      )}
    </li>
  )
}

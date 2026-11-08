import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { toast } from 'sonner'
import { Plus } from 'lucide-react'
import { useParams } from 'react-router'
import { useAccess } from '../access'
import {
  createChatAgent,
  deleteChatAgent,
  listChatAgents,
  messageOf,
  NOTE_TYPES,
  updateChatAgent,
  type AgentFields,
  type AgentRoute,
  type ChatAgent,
  type NoteType,
} from '../api'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
import PageHeader from '@/components/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

// Plan item 8, C4: an admin adds agents to the workspace; the built-in ones are fixed
// (decided 2026-11-08). A deleted agent's chats move to Auto.
const muted = 'text-[13px] text-muted-foreground'
const ROUTES: { value: AgentRoute; label: string }[] = [
  { value: 'auto', label: 'Auto: code, documents or both' },
  { value: 'code', label: 'Code only' },
]
type Fields = AgentFields & { name: string }
const EMPTY: Fields = {
  name: '',
  label: '',
  description: '',
  route: 'auto',
  focus: '',
  note_types: [],
}

export default function Agents() {
  const { workspace = '' } = useParams()
  const admin = useAccess().can('admin')
  const [agents, setAgents] = useState<ChatAgent[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null) // a name, or '' for a new one
  const [confirming, setConfirming] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setAgents(await listChatAgents())
    } catch (e) {
      setError(messageOf(e))
    }
  }, [])

  useEffect(() => {
    if (admin) void load()
  }, [admin, load])

  async function save(fields: Fields) {
    try {
      if (editing) {
        const { name, ...rest } = fields
        await updateChatAgent(name, rest)
      } else {
        await createChatAgent(fields)
      }
      toast.success(`@${fields.name} saved`)
      setError(null)
      setEditing(null)
      await load()
    } catch (e) {
      setError(messageOf(e))
    }
  }

  async function remove(name: string) {
    try {
      await deleteChatAgent(name)
      toast.success(`@${name} deleted`)
      setError(null)
    } catch (e) {
      setError(messageOf(e))
    }
    setConfirming(null)
    await load()
  }

  if (!admin) return <Empty>Only an admin of {workspace} manages its agents.</Empty>

  const builtIn = agents?.filter((a) => !a.custom) ?? []
  const custom = agents?.filter((a) => a.custom) ?? []
  return (
    <section>
      <PageHeader
        title="Agents"
        description={`The agents every member of ${workspace} can chat with. Add one with its own focus; the built-in agents stay as they are.`}
        action={
          editing === null && (
            <Button type="button" size="sm" onClick={() => setEditing('')}>
              <Plus aria-hidden /> New agent
            </Button>
          )
        }
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {editing === '' && (
        <AgentForm initial={EMPTY} isNew onSave={save} onCancel={() => setEditing(null)} />
      )}
      {agents === null ? (
        !error && <Loading rows={3} />
      ) : (
        <>
          <h2 className="mt-6 mb-2 text-base">Custom</h2>
          {custom.length === 0 ? (
            <p className={muted}>No custom agents yet.</p>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Custom agents">
              {custom.map((a) =>
                editing === a.name ? (
                  <li key={a.name}>
                    <AgentForm
                      initial={{
                        name: a.name,
                        label: a.label,
                        description: a.description,
                        route: a.route ?? 'auto',
                        focus: a.focus ?? '',
                        note_types: a.note_types ?? [],
                      }}
                      onSave={save}
                      onCancel={() => setEditing(null)}
                    />
                  </li>
                ) : (
                  <li key={a.name} className="rounded-md border p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{a.label}</span>
                      <code className="text-[13px]">@{a.name}</code>
                      <Badge variant="outline">
                        {ROUTES.find((r) => r.value === a.route)?.label ?? a.route}
                      </Badge>
                      <span className="flex-1" />
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        disabled={editing !== null}
                        onClick={() => setEditing(a.name)}
                      >
                        Edit
                      </Button>
                      {confirming === a.name ? (
                        <span className="flex flex-wrap items-center gap-2 rounded-md bg-warning-bg px-3 py-1 text-[13px] text-warning">
                          Delete @{a.name}? Its chats move to Auto.
                          <Button
                            type="button"
                            size="sm"
                            variant="soft-destructive"
                            onClick={() => void remove(a.name)}
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
                        </span>
                      ) : (
                        <Button
                          type="button"
                          size="sm"
                          variant="soft-destructive"
                          onClick={() => setConfirming(a.name)}
                        >
                          Delete
                        </Button>
                      )}
                    </div>
                    {a.description && <p className="mt-1 mb-0">{a.description}</p>}
                    {a.focus && (
                      <p className={cn(muted, 'mt-2 mb-0 whitespace-pre-wrap')}>Focus: {a.focus}</p>
                    )}
                    {!!a.note_types?.length && (
                      <p className={cn(muted, 'mt-1 mb-0')}>
                        Leans on: {a.note_types.map((t) => NOTE_TYPES[t]).join(', ')}
                      </p>
                    )}
                  </li>
                ),
              )}
            </ul>
          )}
          <h2 className="mt-8 mb-2 text-base">Built in</h2>
          <ul className="m-0 flex list-none flex-col gap-1 p-0" aria-label="Built-in agents">
            {builtIn.map((a) => (
              <li key={a.name}>
                <span className="font-medium">{a.label}</span>{' '}
                <code className="text-[13px]">@{a.name}</code>
                <span className={muted}> · {a.description}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}

function AgentForm(props: {
  initial: Fields
  isNew?: boolean
  onSave: (fields: Fields) => Promise<void>
  onCancel: () => void
}) {
  const [fields, setFields] = useState(props.initial)
  const [busy, setBusy] = useState(false)
  const set = (change: Partial<Fields>) => setFields((f) => ({ ...f, ...change }))
  const id = props.isNew ? 'new-agent' : `agent-${props.initial.name}`

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    await props.onSave({ ...fields, name: fields.name.trim().toLowerCase() })
    setBusy(false)
  }

  function toggle(type: NoteType, on: boolean) {
    const others = fields.note_types.filter((t) => t !== type)
    set({ note_types: on ? [...others, type] : others })
  }

  return (
    <form
      onSubmit={submit}
      aria-label={props.isNew ? 'New agent' : `Edit @${props.initial.name}`}
      className="my-4 flex flex-col gap-3 rounded-md border p-4"
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-name`}>Name (@name)</Label>
          <Input
            id={`${id}-name`}
            value={fields.name}
            disabled={!props.isNew}
            maxLength={24}
            placeholder="security"
            onChange={(e) => set({ name: e.target.value })}
          />
          {props.isNew && (
            <span className={muted}>Lowercase letters, digits or '-'; it cannot change later.</span>
          )}
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-label`}>Label</Label>
          <Input
            id={`${id}-label`}
            value={fields.label}
            maxLength={40}
            placeholder="Security"
            onChange={(e) => set({ label: e.target.value })}
          />
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-description`}>Description</Label>
        <Input
          id={`${id}-description`}
          value={fields.description}
          maxLength={160}
          placeholder="Who may do what, and where it is checked"
          onChange={(e) => set({ description: e.target.value })}
        />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-route`}>Sources</Label>
        <NativeSelect
          id={`${id}-route`}
          value={fields.route}
          onChange={(e) => set({ route: e.target.value as AgentRoute })}
        >
          {ROUTES.map((r) => (
            <NativeSelectOption key={r.value} value={r.value}>
              {r.label}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
      <fieldset className="m-0 flex flex-wrap gap-x-4 gap-y-2 border-0 p-0">
        <legend className="mb-1 text-sm font-medium">Leans on these notes</legend>
        {(Object.keys(NOTE_TYPES) as NoteType[]).map((type) => (
          <Label key={type} className="flex items-center gap-2 font-normal">
            <Checkbox
              checked={fields.note_types.includes(type)}
              onCheckedChange={(on) => toggle(type, on === true)}
            />
            {NOTE_TYPES[type]}
          </Label>
        ))}
      </fieldset>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-focus`}>Focus</Label>
        <Textarea
          id={`${id}-focus`}
          rows={4}
          maxLength={1000}
          value={fields.focus}
          placeholder="Answer for a security reviewer: who may call it, which guard checks it, and what is not checked."
          onChange={(e) => set({ focus: e.target.value })}
        />
        <span className={muted}>
          Added to the answer prompt. Ask for a citation per sentence: a sentence without one is
          tagged unsupported.
        </span>
      </div>
      <div className="flex gap-2">
        <Button type="submit" disabled={busy || !fields.name.trim() || !fields.label.trim()}>
          Save
        </Button>
        <Button type="button" variant="secondary" onClick={props.onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

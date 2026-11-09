import {
  Fragment,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from 'react'
import { useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import {
  ArrowUp,
  Columns3,
  Copy,
  Ellipsis,
  Loader2,
  MessagesSquare,
  Pencil,
  Plus,
  Search,
  Share2,
  Sparkles,
  Trash2,
} from 'lucide-react'
import {
  createChatSession,
  deleteChatSession,
  getChatSession,
  listChatAgents,
  listChatSessions,
  messageOf,
  sendChatMessage,
  updateChatSession,
  type ChatAgent,
  type ChatMessage,
  type ChatSession,
  type ChatSessionDetail,
  type QueryResult,
} from '../api'
import { formatAgo } from '../format'
import Answer from '@/components/Answer'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
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
import { Input } from '@/components/ui/input'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { useWorkspacePath, WsLink } from '@/workspace'

// Plan item 8: conversations with an agent (C2: Technical, Business rules, QA, Overview; Auto
// is what the Ask page did, merged here 2026-11-08). A
// follow-up sees the turns before it, whichever agent gave them; a message starting with @name
// asks another agent, with several @names it is a panel (C3): up to PANEL_MAX agents answer at
// once, side by side. Every answer has a trace. Shared sessions are read-only for others.
const EXAMPLES = [
  'Walk through POST /auth/refresh: what is checked?',
  'Who may call the /users endpoints?',
  'Where is the upload size limit set?',
  'Which documents mention the release gate?',
]
// Until /chat/agents answers (and for an agent it no longer lists).
const FALLBACK_AGENTS: ChatAgent[] = [{ name: 'auto', label: 'Auto', description: '', aliases: [] }]
const muted = 'text-[13px] text-muted-foreground'
const PANEL_MAX = 3 // PANEL_MAX_AGENTS in the API
const MESSAGE_MAX = 1000 // the API's limit on a message
const COUNT_FROM = 900 // characters left are shown from here

// The question being answered: shown until the API stores it, then only the agents still at
// work, each with its latest step.
interface Turn {
  question: string
  pending: string[] // who still answers it
  steps: Record<string, string> // agent -> its latest step
}

export default function Chat() {
  const { id: param } = useParams()
  const id = param ? Number(param) : null
  const navigate = useNavigate()
  const path = useWorkspacePath()
  const [sessions, setSessions] = useState<ChatSession[] | null>(null)
  const [session, setSession] = useState<ChatSessionDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [turn, setTurn] = useState<Turn | null>(null)
  const [agents, setAgents] = useState<ChatAgent[]>(FALLBACK_AGENTS)
  const [picked, setPicked] = useState('auto') // a new chat's agent
  const label = (name: string | null) => agents.find((a) => a.name === name)?.label ?? name ?? ''
  const started = useRef<number | null>(null) // a session this page just made: no reload

  async function loadSessions() {
    try {
      setSessions(await listChatSessions())
    } catch (e) {
      setError(messageOf(e))
    }
  }

  useEffect(() => {
    void loadSessions()
    listChatAgents()
      .then(setAgents)
      .catch(() => {}) // the fallback still chats with Auto
  }, [])

  useEffect(() => {
    setError(null)
    if (id === null) {
      setSession(null)
      return
    }
    if (started.current === id) return
    let current = true
    setSession(null)
    getChatSession(id)
      .then((found) => current && setSession(found))
      .catch((e) => current && setError(messageOf(e)))
    return () => {
      current = false
    }
  }, [id])

  async function send(text: string) {
    setError(null)
    setTurn({ question: text, pending: [session?.agent ?? picked], steps: {} })
    try {
      let target = session
      if (!target) {
        const made = await createChatSession(picked)
        target = { ...made, messages: [] }
        started.current = made.id
        setSession(target)
        navigate(path(`/chat/${made.id}`), { replace: true })
      }
      await sendChatMessage(target.id, text, (e) => {
        if (e.event === 'question') {
          setTurn({ question: '', pending: e.data.answering, steps: {} })
          setSession(
            (s) =>
              s && {
                ...s,
                title: e.data.title,
                messages: [...s.messages, e.data],
              },
          )
        } else if (e.event === 'step') {
          const { agent, label } = e.data
          setTurn((t) => t && { ...t, steps: { ...t.steps, [agent]: label } })
        } else if (e.event === 'answer') {
          const done = e.data.agent
          setTurn((t) => {
            const pending = t?.pending.filter((a) => a !== done) ?? []
            return t && pending.length ? { ...t, pending } : null
          })
          setSession((s) => s && { ...s, messages: [...s.messages, e.data] })
        } else {
          setTurn(null)
          setError(e.data.detail)
        }
      })
    } catch (e) {
      setError(messageOf(e))
    } finally {
      setTurn(null)
      void loadSessions()
    }
  }

  async function change(body: { title?: string; shared?: boolean; agent?: string }) {
    if (!session) return
    try {
      const changed = await updateChatSession(session.id, body)
      setSession({ ...session, ...changed })
      void loadSessions()
    } catch (e) {
      setError(messageOf(e))
    }
  }

  async function remove() {
    if (!session) return
    try {
      await deleteChatSession(session.id)
      toast.success('Chat deleted')
      started.current = null
      navigate(path('/chat'))
      void loadSessions()
    } catch (e) {
      setError(messageOf(e))
    }
  }

  const writable = id === null || !!session?.mine
  return (
    // The page fills the window under the top bar (h-12 + the content's pt-6, its pb-20 taken
    // back): the chats and the conversation scroll on their own, the composer stays in view.
    // On a phone without an open chat, the list and a new chat stack and the page scrolls.
    <section
      className={cn(
        '-mb-20 flex h-[calc(100dvh-4.5rem)] min-h-[24rem] gap-6',
        id === null && 'max-md:mb-0 max-md:h-auto max-md:flex-col',
      )}
    >
      <h1 className="sr-only">Chat</h1>
      <SessionList sessions={sessions} current={id} hidden={id !== null} />
      <div className="flex min-w-0 flex-1 flex-col">
        {id !== null && (
          <WsLink to="/chat" className="mb-2 inline-block shrink-0 text-[13px] md:hidden">
            ← All chats
          </WsLink>
        )}
        {id !== null && !session ? (
          error ? (
            <ErrorAlert>{error}</ErrorAlert>
          ) : (
            <Loading rows={3} />
          )
        ) : (
          <>
            {session && (
              <SessionHeader
                key={session.id}
                session={session}
                agents={agents}
                onChange={change}
                onDelete={remove}
              />
            )}
            <div className="min-h-0 flex-1 overflow-y-auto">
              <div className="mx-auto w-full max-w-3xl py-4">
                {(session?.messages.length ?? 0) > 0 || turn ? (
                  <Conversation messages={session?.messages ?? []} turn={turn} label={label} />
                ) : (
                  <Start
                    agents={agents}
                    picked={session?.agent ?? picked}
                    onAgent={session ? (agent) => void change({ agent }) : setPicked}
                    onPick={(q) => void send(q)}
                  />
                )}
              </div>
            </div>
            <div className="sticky bottom-0 mx-auto w-full max-w-3xl shrink-0 bg-background">
              {error && <ErrorAlert>{error}</ErrorAlert>}
              {writable ? (
                <Composer busy={turn !== null} agents={agents} onSend={(q) => void send(q)} />
              ) : (
                <p className={cn(muted, 'm-0 border-t py-4 text-center')}>
                  Shared by {session?.owner ?? 'someone'}: you can read it, not write in it.
                </p>
              )}
            </div>
          </>
        )}
      </div>
    </section>
  )
}

// The chats under a day heading: today, the six days before, and older.
function byDay(sessions: ChatSession[], now = Date.now()) {
  const today = new Date(now)
  today.setHours(0, 0, 0, 0)
  const start = today.getTime()
  const week = start - 6 * 86_400_000
  const groups = [
    { label: 'Today', items: [] as ChatSession[] },
    { label: 'Previous 7 days', items: [] as ChatSession[] },
    { label: 'Older', items: [] as ChatSession[] },
  ]
  for (const s of sessions) {
    const at = new Date(s.updated_at).getTime()
    groups[at >= start ? 0 : at >= week ? 1 : 2].items.push(s)
  }
  return groups.filter((g) => g.items.length)
}

function SessionList(props: {
  sessions: ChatSession[] | null
  current: number | null
  hidden: boolean // on a phone, an open chat takes the whole width
}) {
  const { sessions, current } = props
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const found = sessions?.filter((s) => s.title.toLowerCase().includes(q)) ?? []
  return (
    <nav
      aria-label="Chats"
      className={cn(
        'flex w-full min-w-0 flex-col md:w-60 md:shrink-0',
        props.hidden && 'max-md:hidden',
      )}
    >
      <Button asChild size="sm" className="mb-2 w-full shrink-0 hover:no-underline">
        <WsLink to="/chat">
          <Plus aria-hidden /> New chat
        </WsLink>
      </Button>
      <div className="relative mb-3 shrink-0">
        <Search
          className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <Input
          type="search"
          aria-label="Search chats"
          placeholder="Search chats"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="h-8 pl-8 text-sm"
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {sessions === null ? (
          <Loading rows={3} />
        ) : sessions.length === 0 ? (
          <p className={muted}>No chats yet: ask something to start one.</p>
        ) : found.length === 0 ? (
          <p className={muted}>{`No chats match “${query.trim()}”.`}</p>
        ) : (
          byDay(found).map((group) => (
            <section key={group.label} className="mb-3">
              <h2 className="m-0 px-2 pb-1 text-[12px] font-medium text-muted-foreground">
                {group.label}
              </h2>
              <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
                {group.items.map((s) => (
                  <li key={s.id}>
                    <WsLink
                      to={`/chat/${s.id}`}
                      aria-current={s.id === current ? 'page' : undefined}
                      className={cn(
                        'block rounded-md px-2 py-1.5 text-foreground hover:bg-accent hover:no-underline',
                        s.id === current && 'bg-accent',
                      )}
                    >
                      <span className="flex items-center gap-1.5">
                        <span className="truncate text-sm">{s.title}</span>
                        {s.shared && (
                          <Share2 className="size-3 shrink-0 text-muted-foreground" aria-hidden />
                        )}
                      </span>
                      <span className="block text-[12px] text-muted-foreground">
                        {formatAgo(s.updated_at)}
                        {!s.mine ? ` · ${s.owner ?? 'shared'}` : s.shared ? ' · shared' : ''}
                      </span>
                    </WsLink>
                  </li>
                ))}
              </ul>
            </section>
          ))
        )}
      </div>
    </nav>
  )
}

function SessionHeader(props: {
  session: ChatSessionDetail
  agents: ChatAgent[]
  onChange: (body: { title?: string; shared?: boolean; agent?: string }) => Promise<void>
  onDelete: () => Promise<void>
}) {
  const { session } = props
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(session.title)
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const actions = useRef<HTMLButtonElement>(null) // the ⋯ button: focus comes back to it
  const handOff = useRef(false) // a menu item moved focus on (title field, dialog)

  async function confirmDelete() {
    setDeleting(true)
    try {
      await props.onDelete()
    } finally {
      setDeleting(false)
    }
  }

  async function rename(event: FormEvent) {
    event.preventDefault()
    if (title.trim()) await props.onChange({ title: title.trim() })
    setRenaming(false)
  }

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b pb-3">
      {renaming ? (
        <form onSubmit={rename} className="flex min-w-0 flex-1 gap-2">
          <Input
            aria-label="Chat title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="min-w-0 flex-1"
            autoFocus
          />
          <Button type="submit" size="sm">
            Save
          </Button>
          <Button type="button" size="sm" variant="secondary" onClick={() => setRenaming(false)}>
            Cancel
          </Button>
        </form>
      ) : (
        <h2 className="m-0 min-w-0 flex-1 truncate text-base max-md:basis-full">{session.title}</h2>
      )}
      {session.mine ? (
        <NativeSelect
          size="sm"
          aria-label="Agent"
          title="Answers every message that names no @agent"
          value={session.agent}
          onChange={(e) => void props.onChange({ agent: e.target.value })}
        >
          {props.agents.map((a) => (
            <NativeSelectOption key={a.name} value={a.name}>
              {a.label}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      ) : (
        <Badge variant="outline">
          {props.agents.find((a) => a.name === session.agent)?.label ?? session.agent}
        </Badge>
      )}
      {session.shared && (
        <Badge variant="secondary">
          <Share2 aria-hidden /> Shared
        </Badge>
      )}
      {session.mine && !renaming && (
        <>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            aria-pressed={session.shared}
            title="Every member of this workspace can read it, not write in it"
            onClick={() => void props.onChange({ shared: !session.shared })}
          >
            <Share2 aria-hidden />
            {session.shared ? 'Stop sharing' : 'Share'}
          </Button>
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button
                ref={actions}
                type="button"
                size="icon-sm"
                variant="ghost"
                aria-label="Chat actions"
              >
                <Ellipsis aria-hidden />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              onCloseAutoFocus={(e) => {
                if (handOff.current) e.preventDefault() // keep it in the title field or dialog
                handOff.current = false
              }}
            >
              <DropdownMenuItem
                onSelect={() => {
                  handOff.current = true
                  setTitle(session.title)
                  setRenaming(true)
                }}
              >
                <Pencil aria-hidden /> Rename
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                variant="destructive"
                onSelect={() => {
                  handOff.current = true
                  setConfirming(true)
                }}
              >
                <Trash2 aria-hidden /> Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <Dialog open={confirming} onOpenChange={setConfirming}>
            <DialogContent
              className="sm:max-w-md"
              onCloseAutoFocus={(e) => {
                e.preventDefault() // opened from the menu, it gives focus back to the ⋯ button
                actions.current?.focus()
              }}
            >
              <DialogTitle>Delete this chat?</DialogTitle>
              <DialogDescription>
                “{session.title}” and its answers are deleted
                {session.shared ? ', also for the members it is shared with' : ''}. This cannot be
                undone.
              </DialogDescription>
              <DialogFooter>
                <Button type="button" variant="secondary" onClick={() => setConfirming(false)}>
                  Cancel
                </Button>
                <Button
                  type="button"
                  variant="destructive"
                  disabled={deleting}
                  onClick={() => void confirmDelete()}
                >
                  Delete
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </>
      )}
    </div>
  )
}

function asResult(m: ChatMessage): QueryResult {
  return {
    question: '',
    answer: m.text,
    sources: m.sources ?? [],
    chunks: [],
    entities: [],
    relations: [],
    found: m.found ?? true,
    query_secs: 0,
    mode: 'enigram-agent',
  }
}

// A question and the answers to it: one, or a panel's.
interface Exchange {
  question: ChatMessage | null // null: answers before any stored question (not expected)
  answers: ChatMessage[]
}

function exchanges(messages: ChatMessage[]): Exchange[] {
  const out: Exchange[] = []
  for (const m of messages) {
    if (m.role === 'user') out.push({ question: m, answers: [] })
    else if (out.length) out[out.length - 1].answers.push(m)
    else out.push({ question: null, answers: [m] })
  }
  return out
}

function Conversation(props: {
  messages: ChatMessage[]
  turn: Turn | null
  label: (agent: string | null) => string
}) {
  const { messages, turn, label } = props
  const end = useRef<HTMLLIElement>(null)
  const stepCount = Object.keys(turn?.steps ?? {}).length
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: 'end', behavior: 'smooth' })
  }, [messages.length, stepCount, turn?.pending.length])

  const all = exchanges(messages)
  return (
    <ol className="m-0 flex list-none flex-col gap-5 p-0" aria-label="Conversation">
      {all.map((x, i) => {
        // The last question's agents still at work (none once the API has stored nothing yet).
        const pending = turn && !turn.question && i === all.length - 1 ? turn.pending : []
        return (
          <Fragment key={x.question?.id ?? `a${x.answers[0]?.id}`}>
            {x.question && (
              <li className="ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-muted px-4 py-2.5">
                <span className="sr-only">You: </span>
                <span className="whitespace-pre-wrap">{x.question.text}</span>
              </li>
            )}
            {x.answers.length + pending.length > 1 ? (
              <li className="min-w-0">
                <Panel
                  answers={x.answers}
                  pending={pending}
                  steps={turn?.steps ?? {}}
                  label={label}
                />
              </li>
            ) : (
              <>
                {x.answers.map((m) => (
                  <li key={m.id} className="min-w-0">
                    <AgentAnswer message={m} label={label} />
                  </li>
                ))}
                {pending.map((agent) => (
                  <li key={agent} role="status">
                    <Working agent={agent} step={turn?.steps[agent]} label={label} />
                  </li>
                ))}
              </>
            )}
          </Fragment>
        )
      })}
      {turn?.question && (
        <li role="status" className="flex flex-col gap-5">
          <span className="ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-muted px-4 py-2.5 whitespace-pre-wrap">
            {turn.question}
          </span>
          {turn.pending.map((agent) => (
            <Working key={agent} agent={agent} step={turn.steps[agent]} label={label} />
          ))}
        </li>
      )}
      <li ref={end} aria-hidden />
    </ol>
  )
}

// Who answers: one mark for every agent, its name next to it.
function AgentName({ children }: { children: string }) {
  return (
    <span className="flex items-center gap-2 text-[13px] font-medium">
      <span className="flex size-6 items-center justify-center rounded-md border bg-card">
        <Sparkles className="size-3.5" aria-hidden />
      </span>
      <span>{children}</span>
    </span>
  )
}

function AgentAnswer(props: { message: ChatMessage; label: (agent: string | null) => string }) {
  const m = props.message
  async function copy() {
    try {
      await navigator.clipboard.writeText(m.text)
      toast.success('Copied')
    } catch {
      toast.error('Could not copy: select the text instead')
    }
  }
  return (
    <>
      <AgentName>{props.label(m.agent)}</AgentName>
      {m.error ? (
        <div className="mt-2">
          <ErrorAlert>{m.error}</ErrorAlert>
        </div>
      ) : (
        <div className="[&>div]:mt-1">
          <Answer result={asResult(m)} />
        </div>
      )}
      <p className={cn(muted, 'mt-1 mb-0 flex flex-wrap items-center gap-x-3 gap-y-1')}>
        {!m.error && m.text && (
          <Button
            type="button"
            size="xs"
            variant="ghost"
            aria-label="Copy answer"
            className="-ml-2 text-muted-foreground"
            onClick={() => void copy()}
          >
            <Copy aria-hidden /> Copy
          </Button>
        )}
        {m.query_id !== null && <WsLink to={`/trace/${m.query_id}`}>View trace</WsLink>}
        {m.tokens > 0 && <span>{m.tokens.toLocaleString('en-US')} tokens</span>}
      </p>
    </>
  )
}

function Working(props: {
  agent: string
  step: string | undefined
  label: (agent: string | null) => string
}) {
  return (
    <span className="flex flex-col gap-2">
      <AgentName>{props.label(props.agent)}</AgentName>
      <span className={cn(muted, 'flex items-center gap-2 pl-8')}>
        <Loader2 className="size-3.5 animate-spin text-ring" aria-hidden />
        {props.step ?? 'Thinking'}…
      </span>
    </span>
  )
}

// A panel's answers: side by side on a wide screen, one tab each below that.
function Panel(props: {
  answers: ChatMessage[]
  pending: string[]
  steps: Record<string, string>
  label: (agent: string | null) => string
}) {
  const { answers, pending, label } = props
  const agents = [...answers.map((m) => m.agent ?? ''), ...pending]
  const [shown, setShown] = useState(agents[0])
  const current = agents.includes(shown) ? shown : agents[0]
  const total = answers.reduce((sum, m) => sum + m.tokens, 0)
  return (
    <section aria-label="Panel" className="rounded-lg border p-4">
      <div
        role="tablist"
        aria-label="Panel answers"
        className="mb-3 flex flex-wrap gap-1 lg:hidden"
      >
        {agents.map((agent) => (
          <Button
            key={agent}
            type="button"
            role="tab"
            size="sm"
            aria-selected={agent === current}
            variant={agent === current ? 'default' : 'secondary'}
            onClick={() => setShown(agent)}
          >
            {label(agent)}
          </Button>
        ))}
      </div>
      <div className={cn('grid gap-4', agents.length === 3 ? 'lg:grid-cols-3' : 'lg:grid-cols-2')}>
        {answers.map((m) => (
          <div key={m.id} className={cn('min-w-0', m.agent !== current && 'max-lg:hidden')}>
            <AgentAnswer message={m} label={label} />
          </div>
        ))}
        {pending.map((agent) => (
          <div key={agent} role="status" className={cn(agent !== current && 'max-lg:hidden')}>
            <Working agent={agent} step={props.steps[agent]} label={label} />
          </div>
        ))}
      </div>
      {!pending.length && total > 0 && (
        <p className={cn(muted, 'mt-3 mb-0')}>Panel: {total.toLocaleString('en-US')} tokens</p>
      )}
    </section>
  )
}

function Start(props: {
  agents: ChatAgent[]
  picked: string
  onAgent: (agent: string) => void
  onPick: (question: string) => void
}) {
  const id = useId()
  return (
    <div className="flex flex-col items-center gap-6 pt-6 text-center sm:pt-12">
      <div className="flex flex-col items-center gap-2">
        <span className="flex size-10 items-center justify-center rounded-lg border bg-card">
          <MessagesSquare className="size-5" aria-hidden />
        </span>
        <h2 className="m-0">Pick an agent and ask</h2>
        <p className="m-0 max-w-[56ch] text-muted-foreground">
          Each agent answers from the indexed code and documents with its own focus, and remembers
          what was asked before in this chat.
        </p>
      </div>
      <div
        role="radiogroup"
        aria-label="Agent"
        className="grid w-full gap-2 sm:grid-cols-2 lg:grid-cols-3"
      >
        {props.agents.map((a) => {
          const on = props.picked === a.name
          return (
            <button
              key={a.name}
              type="button"
              role="radio"
              aria-checked={on}
              aria-labelledby={`${id}-${a.name}`}
              aria-describedby={a.description ? `${id}-${a.name}-d` : undefined}
              onClick={() => props.onAgent(a.name)}
              className={cn(
                'flex cursor-pointer flex-col gap-0.5 rounded-lg border px-3 py-2.5 text-left transition-colors outline-none hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50',
                on && 'border-foreground bg-accent',
              )}
            >
              <span id={`${id}-${a.name}`} className="text-sm font-medium">
                {a.label}
              </span>
              {a.description && (
                <span id={`${id}-${a.name}-d`} className="text-[13px] text-muted-foreground">
                  {a.description}
                </span>
              )}
            </button>
          )
        })}
      </div>
      <div className="w-full text-left">
        <p className={cn(muted, 'mt-0 mb-2')}>Try one of these:</p>
        <div className="grid gap-2 sm:grid-cols-2">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => props.onPick(example)}
              className="cursor-pointer rounded-lg border bg-card px-3 py-2.5 text-left text-sm transition-colors outline-none hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              {example}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function Composer(props: { busy: boolean; agents: ChatAgent[]; onSend: (text: string) => void }) {
  const { busy, agents, onSend } = props
  const mentions = agents.map((a) => `@${a.name}`).join(', ')
  const [text, setText] = useState('')
  // A panel for the next message: the agents picked here are put in front of it as @names.
  const [panel, setPanel] = useState<string[] | null>(null)

  function submit(event?: FormEvent) {
    event?.preventDefault()
    const question = text.trim()
    if (!question || busy) return
    setText('')
    setPanel(null) // a panel is for one message
    const names = panel ?? []
    onSend(names.length ? `${names.map((n) => `@${n}`).join(' ')} ${question}` : question)
  }

  function toggle(name: string) {
    setPanel((p) => (p?.includes(name) ? p.filter((n) => n !== name) : [...(p ?? []), name]))
  }

  // Enter sends; Shift+Enter starts a new line (and Enter while composing text in an IME).
  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <form onSubmit={submit} className="pt-2 pb-4">
      <div className="rounded-xl border bg-background shadow-xs transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/25">
        {panel && (
          <div
            role="group"
            aria-label="Panel agents"
            className="flex flex-wrap items-center gap-1.5 border-b px-3 py-2"
          >
            <span className={cn(muted, 'mr-1')}>
              Ask up to {PANEL_MAX} agents at once ({panel.length} picked):
            </span>
            {agents.map((a) => {
              const on = panel.includes(a.name)
              return (
                <Button
                  key={a.name}
                  type="button"
                  size="xs"
                  aria-pressed={on}
                  variant={on ? 'default' : 'secondary'}
                  disabled={!on && panel.length >= PANEL_MAX}
                  title={a.description}
                  onClick={() => toggle(a.name)}
                >
                  {a.label}
                </Button>
              )
            })}
          </div>
        )}
        {/* field-sizing-content (in Textarea): it grows with the text, up to max-h, then scrolls. */}
        <Textarea
          aria-label="Message"
          rows={1}
          maxLength={MESSAGE_MAX}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask, ask on about the last answer, or start with @qa to ask another agent"
          className="max-h-44 min-h-11 resize-none rounded-none border-0 bg-transparent px-3.5 pt-3 pb-1 text-sm shadow-none focus-visible:ring-0 dark:bg-transparent"
        />
        <div className="flex items-center gap-2 px-2 pb-2">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            aria-pressed={panel !== null}
            title="One question to several agents at once; each answer costs its own tokens"
            className="text-muted-foreground aria-pressed:bg-accent aria-pressed:text-foreground"
            onClick={() => setPanel((p) => (p ? null : []))}
          >
            <Columns3 aria-hidden /> Panel
          </Button>
          <span className="flex-1" />
          {text.length >= COUNT_FROM && (
            <span
              className={cn(
                'text-[12px] tabular-nums',
                text.length >= MESSAGE_MAX ? 'text-destructive' : 'text-muted-foreground',
              )}
            >
              {`${text.length} / ${MESSAGE_MAX}`}
            </span>
          )}
          <Button
            type="submit"
            size="icon-sm"
            aria-label="Send"
            className="rounded-lg"
            disabled={busy || !text.trim() || panel?.length === 1}
          >
            {busy ? <Loader2 className="animate-spin" aria-hidden /> : <ArrowUp aria-hidden />}
          </Button>
        </div>
      </div>
      <p className="mt-1.5 mb-0 text-center text-[12px] text-muted-foreground max-sm:hidden">
        Enter to send · Shift+Enter for a new line · {mentions} for one message, several for a panel
      </p>
    </form>
  )
}

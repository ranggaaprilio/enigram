import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { Loader2, MessagesSquare, Plus, Share2 } from 'lucide-react'
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
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
import PageHeader from '@/components/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { useWorkspacePath, WsLink } from '@/workspace'

// Plan item 8: conversations with an agent (C2: Technical, Business rules, QA, Overview). A
// follow-up sees the turns before it, whichever agent gave them; a message starting with @name
// asks another agent. Every answer has a trace. Shared sessions are read-only for others.
const EXAMPLES = [
  'Walk through POST /auth/refresh: what is checked?',
  'Who may call the /users endpoints?',
  'Where is the upload size limit set?',
]
// Until /chat/agents answers (and for an agent it no longer lists).
const FALLBACK_AGENTS: ChatAgent[] = [
  { name: 'technical', label: 'Technical', description: '', aliases: [] },
]
const muted = 'text-[13px] text-muted-foreground'

// The question being answered: shown until the API stores it, then only its steps.
interface Turn {
  question: string
  agent: string // who answers it
  steps: string[]
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
  const [picked, setPicked] = useState('technical') // a new chat's agent
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
      .catch(() => {}) // the fallback still chats with Technical
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
    setTurn({ question: text, agent: session?.agent ?? picked, steps: [] })
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
          setTurn({ question: '', agent: e.data.answering, steps: [] })
          setSession((s) => s && { ...s, title: e.data.title, messages: [...s.messages, e.data] })
        } else if (e.event === 'step') {
          setTurn((t) => t && { ...t, steps: [...t.steps, e.data.label] })
        } else if (e.event === 'answer') {
          setTurn(null)
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
    <section>
      <PageHeader
        title="Chat"
        description="A conversation with an agent: ask, then ask on. Each agent has its own focus; every answer cites its sources and has a trace."
      />
      <div className="grid gap-6 md:grid-cols-[15rem_minmax(0,1fr)]">
        <SessionList sessions={sessions} current={id} hidden={id !== null} />
        <div className="min-w-0">
          {id !== null && (
            <WsLink to="/chat" className="mb-3 inline-block text-[13px] md:hidden">
              ← All chats
            </WsLink>
          )}
          {error && <ErrorAlert>{error}</ErrorAlert>}
          {id !== null && !session ? (
            !error && <Loading rows={3} />
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
              {writable ? (
                <Composer busy={turn !== null} agents={agents} onSend={(q) => void send(q)} />
              ) : (
                <p className={cn(muted, 'mt-6')}>
                  Shared by {session?.owner ?? 'someone'}: you can read it, not write in it.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  )
}

function SessionList(props: {
  sessions: ChatSession[] | null
  current: number | null
  hidden: boolean // on a phone, an open chat takes the whole width
}) {
  const { sessions, current } = props
  return (
    <nav aria-label="Chats" className={cn('min-w-0', props.hidden && 'max-md:hidden')}>
      <Button asChild size="sm" className="mb-3 w-full hover:no-underline">
        <WsLink to="/chat">
          <Plus aria-hidden /> New chat
        </WsLink>
      </Button>
      {sessions === null ? (
        <Loading rows={3} />
      ) : sessions.length === 0 ? (
        <p className={muted}>No chats yet.</p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-1 p-0">
          {sessions.map((s) => (
            <li key={s.id}>
              <WsLink
                to={`/chat/${s.id}`}
                aria-current={s.id === current ? 'page' : undefined}
                className={cn(
                  'block rounded-md px-2 py-1.5 text-foreground hover:bg-accent hover:no-underline',
                  s.id === current && 'bg-accent',
                )}
              >
                <span className="line-clamp-2 text-sm">{s.title}</span>
                <span className="text-[12px] text-muted-foreground">
                  {formatAgo(s.updated_at)}
                  {!s.mine ? ` · ${s.owner ?? 'shared'}` : s.shared ? ' · shared' : ''}
                </span>
              </WsLink>
            </li>
          ))}
        </ul>
      )}
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

  async function rename(event: FormEvent) {
    event.preventDefault()
    if (title.trim()) await props.onChange({ title: title.trim() })
    setRenaming(false)
  }

  return (
    <div className="mb-4 flex flex-wrap items-center gap-2 border-b pb-3">
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
        <h2 className="m-0 min-w-0 flex-1 truncate text-base">{session.title}</h2>
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
            variant="secondary"
            onClick={() => {
              setTitle(session.title)
              setRenaming(true)
            }}
          >
            Rename
          </Button>
          <Button
            type="button"
            size="sm"
            variant="secondary"
            aria-pressed={session.shared}
            title="Every member of this workspace can read it, not write in it"
            onClick={() => void props.onChange({ shared: !session.shared })}
          >
            {session.shared ? 'Stop sharing' : 'Share'}
          </Button>
          {confirming ? (
            <span className="flex items-center gap-2 rounded-md bg-warning-bg px-3 py-1 text-[13px] text-warning">
              Delete this chat?
              <Button
                type="button"
                size="sm"
                variant="soft-destructive"
                onClick={() => void props.onDelete()}
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

function Conversation(props: {
  messages: ChatMessage[]
  turn: Turn | null
  label: (agent: string | null) => string
}) {
  const { messages, turn, label } = props
  const end = useRef<HTMLLIElement>(null)
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: 'end', behavior: 'smooth' })
  }, [messages.length, turn?.steps.length])

  return (
    <ol className="m-0 flex list-none flex-col gap-5 p-0" aria-label="Conversation">
      {messages.map((m) =>
        m.role === 'user' ? (
          <li key={m.id} className="ml-auto max-w-[85%] rounded-md bg-muted px-4 py-2.5">
            <span className="sr-only">You: </span>
            <span className="whitespace-pre-wrap">{m.text}</span>
          </li>
        ) : (
          <li key={m.id} className="min-w-0">
            <p className={cn(muted, 'm-0 font-medium')}>{label(m.agent)}</p>
            {m.error ? (
              <ErrorAlert>{m.error}</ErrorAlert>
            ) : (
              <div className="[&>div]:mt-1">
                <Answer result={asResult(m)} />
              </div>
            )}
            <p className={cn(muted, 'mt-2 mb-0 flex flex-wrap gap-3')}>
              {m.tokens > 0 && <span>{m.tokens.toLocaleString('en-US')} tokens</span>}
              {m.query_id !== null && <WsLink to={`/trace/${m.query_id}`}>View trace</WsLink>}
            </p>
          </li>
        ),
      )}
      {turn && (
        <li role="status" className={cn(muted, 'flex flex-col gap-2')}>
          {turn.question && (
            <span className="ml-auto max-w-[85%] rounded-md bg-muted px-4 py-2.5 text-sm whitespace-pre-wrap text-foreground">
              {turn.question}
            </span>
          )}
          <span className="flex items-center gap-2">
            <Loader2 className="size-4 animate-spin text-ring" aria-hidden />
            {label(turn.agent)}: {turn.steps.at(-1) ?? 'Thinking'}…
          </span>
        </li>
      )}
      <li ref={end} aria-hidden />
    </ol>
  )
}

function Start(props: {
  agents: ChatAgent[]
  picked: string
  onAgent: (agent: string) => void
  onPick: (question: string) => void
}) {
  return (
    <Empty
      action={
        <div className="flex flex-col items-center gap-4">
          <div role="radiogroup" aria-label="Agent" className="flex flex-wrap justify-center gap-2">
            {props.agents.map((a) => (
              <Button
                key={a.name}
                type="button"
                role="radio"
                aria-checked={props.picked === a.name}
                variant={props.picked === a.name ? 'default' : 'secondary'}
                size="sm"
                title={a.description}
                onClick={() => props.onAgent(a.name)}
              >
                {a.label}
              </Button>
            ))}
          </div>
          <p className="m-0 max-w-[60ch] text-[13px]">
            {props.agents.find((a) => a.name === props.picked)?.description}
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            {EXAMPLES.map((example) => (
              <Button
                key={example}
                type="button"
                variant="outline"
                size="sm"
                className="h-auto min-h-8 py-1.5 whitespace-normal"
                onClick={() => props.onPick(example)}
              >
                {example}
              </Button>
            ))}
          </div>
        </div>
      }
    >
      <span className="mb-2 flex items-center justify-center gap-2 font-medium text-foreground">
        <MessagesSquare className="size-4" aria-hidden /> Pick an agent and ask
      </span>
      Each agent answers from the indexed code and documents with its own focus, and remembers what
      was asked before in this chat. Try one of these:
    </Empty>
  )
}

function Composer(props: { busy: boolean; agents: ChatAgent[]; onSend: (text: string) => void }) {
  const { busy, onSend } = props
  const mentions = props.agents.map((a) => `@${a.name}`).join(', ')
  const [text, setText] = useState('')

  function submit(event?: FormEvent) {
    event?.preventDefault()
    const question = text.trim()
    if (!question || busy) return
    setText('')
    onSend(question)
  }

  // Enter sends; Shift+Enter starts a new line (and Enter while composing text in an IME).
  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <form onSubmit={submit} className="mt-6 flex flex-col gap-2">
      <Textarea
        aria-label="Message"
        rows={3}
        maxLength={1000}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Ask, ask on about the last answer, or start with @qa to ask another agent"
      />
      <div className="flex items-center gap-3">
        <Button type="submit" disabled={busy || !text.trim()}>
          Send
        </Button>
        <span className={cn(muted, 'max-sm:hidden')}>
          Enter to send · Shift+Enter for a new line · {mentions} for one message
        </span>
      </div>
    </form>
  )
}

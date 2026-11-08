import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { ChatMessage, ChatSession } from '../api'
import { asRole } from '../test/access'
import { mockFetch, type Reply } from '../test/mockFetch'
import Chat from './Chat'

const session = (id: number, overrides: Partial<ChatSession> = {}): ChatSession => ({
  id,
  owner: 'me',
  title: `Chat ${id} title`,
  agent: 'technical',
  shared: false,
  created_at: '2026-11-08T10:00:00Z',
  updated_at: '2026-11-08T10:05:00Z',
  mine: true,
  ...overrides,
})

const message = (id: number, overrides: Partial<ChatMessage>): ChatMessage => ({
  id,
  session_id: 1,
  role: 'user',
  agent: null,
  text: '',
  query_id: null,
  tokens: 0,
  error: null,
  created_at: '2026-11-08T10:00:00Z',
  ...overrides,
})

const ANSWER = message(2, {
  role: 'agent',
  agent: 'technical',
  text: 'Only admins may call it src/users/users.controller.ts:39-47@70ae60dbb609 [code].',
  query_id: 41,
  tokens: 6463,
  sources: [
    {
      ref: 1,
      title: 'src/users/users.controller.ts:39-47@70ae60dbb609',
      section: 'code',
      pages: '',
      citation: 'src/users/users.controller.ts:39-47@70ae60dbb609',
      origin: 'code',
    },
  ],
  found: true,
})
const QUESTION = message(1, { text: 'Who may call /users?' })
const AGENTS = [
  { name: 'auto', label: 'Auto', description: 'Picks for you', aliases: ['ask'] },
  { name: 'technical', label: 'Technical', description: 'Code flows', aliases: ['tech'] },
  { name: 'business', label: 'Business rules', description: 'Plain rules', aliases: [] },
  { name: 'qa', label: 'QA', description: 'Test scenarios', aliases: [] },
  { name: 'overview', label: 'Overview', description: 'A summary', aliases: [] },
]

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>
}

// The send route answers with server-sent events, which mockFetch (JSON only) cannot.
function withStream(routes: Record<string, Reply>, url: string, events: [string, unknown][]) {
  const fetch = mockFetch(routes)
  const json = fetch.getMockImplementation()!
  const body = events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join('')
  fetch.mockImplementation(async (u: string, init?: RequestInit) =>
    u === url && init?.method === 'POST'
      ? new Response(body, { headers: { 'content-type': 'text/event-stream' } })
      : json(u, init),
  )
  return fetch
}

function renderAt(url: string) {
  render(
    asRole(
      'viewer',
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/chat/:id?" element={<Chat />} />
        </Routes>
        <Where />
      </MemoryRouter>,
    ),
  )
}

const calls = (fetch: ReturnType<typeof mockFetch>) =>
  fetch.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${url}`)

describe('Chat', () => {
  it('opens a session with its answer, sources and trace', async () => {
    mockFetch({
      'GET /api/chat/sessions': {
        body: [session(1), session(2, { mine: false, owner: 'bob@example.com', shared: true })],
      },
      'GET /api/chat/sessions/1': { body: { ...session(1), messages: [QUESTION, ANSWER] } },
    })
    renderAt('/chat/1')
    const messages = await screen.findByRole('list', { name: 'Conversation' })
    expect(within(messages).getByText('Who may call /users?')).toBeInTheDocument()
    expect(within(messages).getByText('code')).toHaveClass('badge-code')
    expect(within(messages).getByRole('link', { name: 'View trace' })).toHaveAttribute(
      'href',
      '/trace/41',
    )
    expect(within(messages).getByText('6,463 tokens')).toBeInTheDocument()
    const chats = screen.getByRole('navigation', { name: 'Chats' })
    expect(within(chats).getByRole('link', { name: /Chat 1 title/ })).toHaveAttribute(
      'aria-current',
      'page',
    )
    expect(within(chats).getByText(/bob@example.com/)).toBeInTheDocument()
    expect(screen.getByLabelText('Message')).toBeInTheDocument()
  })

  it("reads someone else's shared session without writing in it", async () => {
    const theirs = session(2, { mine: false, owner: 'bob@example.com', shared: true })
    mockFetch({
      'GET /api/chat/sessions': { body: [theirs] },
      'GET /api/chat/sessions/2': { body: { ...theirs, messages: [QUESTION, ANSWER] } },
    })
    renderAt('/chat/2')
    expect(await screen.findByText(/Shared by bob@example.com/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Message')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull()
  })

  it('starts a chat with the first question and shows its steps, then the answer', async () => {
    const fetch = withStream(
      {
        'GET /api/chat/sessions': { body: [] },
        'POST /api/chat/sessions': { status: 201, body: session(7, { title: 'New chat' }) },
      },
      '/api/chat/sessions/7/messages',
      [
        ['question', { ...QUESTION, session_id: 7, title: 'Who may call /users?' }],
        ['step', { tool: 'find_symbol', label: 'Looking up a symbol' }],
        ['answer', { ...ANSWER, session_id: 7 }],
      ],
    )
    renderAt('/chat')
    expect(await screen.findByText(/Pick an agent and ask/)).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Message'), 'Who may call /users?{Enter}')

    expect(await screen.findByRole('link', { name: 'View trace' })).toBeInTheDocument()
    expect(screen.getByTestId('where')).toHaveTextContent('/chat/7')
    expect(screen.getByRole('heading', { name: 'Who may call /users?' })).toBeInTheDocument()
    const post = fetch.mock.calls.find(([u]) => u === '/api/chat/sessions/7/messages')!
    expect(JSON.parse(String(post[1]?.body))).toEqual({ text: 'Who may call /users?' })
    expect(screen.getByLabelText('Message')).toHaveValue('')
    await waitFor(() =>
      expect(calls(fetch).filter((c) => c === 'GET /api/chat/sessions')).toHaveLength(2),
    )
  })

  it('shows a failed answer as an error and keeps the question', async () => {
    withStream(
      {
        'GET /api/chat/sessions': { body: [session(1)] },
        'GET /api/chat/sessions/1': { body: { ...session(1), messages: [] } },
      },
      '/api/chat/sessions/1/messages',
      [
        ['question', { ...QUESTION, title: 'Chat 1 title' }],
        [
          'answer',
          message(2, { role: 'agent', agent: 'technical', error: 'Daily budget used up' }),
        ],
      ],
    )
    renderAt('/chat/1')
    await userEvent.type(await screen.findByLabelText('Message'), 'Who may call /users?')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText('Daily budget used up')).toBeInTheDocument()
    expect(screen.getByText('Who may call /users?')).toBeInTheDocument()
  })

  it('lets the owner rename, share and delete', async () => {
    const fetch = mockFetch({
      'GET /api/chat/sessions': { body: [session(1)] },
      'GET /api/chat/sessions/1': { body: { ...session(1), messages: [QUESTION, ANSWER] } },
      'PATCH /api/chat/sessions/1': { body: session(1, { title: 'Users access' }) },
      'DELETE /api/chat/sessions/1': { body: null },
    })
    renderAt('/chat/1')
    await userEvent.click(await screen.findByRole('button', { name: 'Rename' }))
    const title = screen.getByLabelText('Chat title')
    await userEvent.clear(title)
    await userEvent.type(title, 'Users access{Enter}')
    expect(await screen.findByRole('heading', { name: 'Users access' })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Share' }))
    const patches = fetch.mock.calls.filter(([, init]) => init?.method === 'PATCH')
    expect(patches.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { title: 'Users access' },
      { shared: true },
    ])

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(calls(fetch)).not.toContain('DELETE /api/chat/sessions/1')
    await userEvent.click(screen.getByRole('button', { name: 'Yes' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(/^\/chat$/))
    expect(calls(fetch)).toContain('DELETE /api/chat/sessions/1')
  })

  it('names a session it cannot open', async () => {
    mockFetch({
      'GET /api/chat/sessions': { body: [] },
      'GET /api/chat/sessions/9': { status: 404, body: { detail: 'No chat session 9' } },
    })
    renderAt('/chat/9')
    expect(await screen.findByText('No chat session 9')).toBeInTheDocument()
    vi.unstubAllGlobals()
  })
})

describe('Chat agents', () => {
  it('starts a chat with the picked agent and labels each answer by its agent', async () => {
    const fetch = withStream(
      {
        'GET /api/chat/agents': { body: AGENTS },
        'GET /api/chat/sessions': { body: [] },
        'POST /api/chat/sessions': { status: 201, body: session(8, { agent: 'qa' }) },
      },
      '/api/chat/sessions/8/messages',
      [
        ['question', { ...QUESTION, session_id: 8, title: 'Q', answering: 'qa' }],
        ['answer', { ...ANSWER, session_id: 8, agent: 'qa' }],
      ],
    )
    renderAt('/chat')
    const group = await screen.findByRole('radiogroup', { name: 'Agent' })
    // A new chat starts with Auto, what the Ask page did.
    expect(await within(group).findByRole('radio', { name: 'Auto' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    await userEvent.click(await within(group).findByRole('radio', { name: 'QA' }))
    expect(within(group).getByRole('radio', { name: 'QA' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText('Test scenarios')).toBeInTheDocument()
    expect(screen.getByText(/@auto, @technical, @business, @qa, @overview/)).toBeInTheDocument()

    await userEvent.type(screen.getByRole('textbox', { name: 'Message' }), 'Scenarios?{Enter}')
    const conversation = await screen.findByRole('list', { name: 'Conversation' })
    expect(await within(conversation).findByText('QA')).toBeInTheDocument()
    const create = fetch.mock.calls.find(([u, i]) => u === '/api/chat/sessions' && i?.method)!
    expect(JSON.parse(String(create[1]?.body))).toEqual({ agent: 'qa' })
    expect(screen.getByRole('combobox', { name: 'Agent' })).toHaveValue('qa')
  })

  it("changes the session's agent", async () => {
    const fetch = mockFetch({
      'GET /api/chat/agents': { body: AGENTS },
      'GET /api/chat/sessions': { body: [session(1)] },
      'GET /api/chat/sessions/1': { body: { ...session(1), messages: [QUESTION, ANSWER] } },
      'PATCH /api/chat/sessions/1': { body: session(1, { agent: 'business' }) },
    })
    renderAt('/chat/1')
    const select = await screen.findByRole('combobox', { name: 'Agent' })
    await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(5))
    await userEvent.selectOptions(select, 'business')
    await waitFor(() => expect(select).toHaveValue('business'))
    const patch = fetch.mock.calls.find(([, i]) => i?.method === 'PATCH')!
    expect(JSON.parse(String(patch[1]?.body))).toEqual({ agent: 'business' })
    // Answers keep the label of the agent that gave them.
    expect(within(screen.getByRole('list', { name: 'Conversation' })).getByText('Technical')).toBeInTheDocument()
  })
})

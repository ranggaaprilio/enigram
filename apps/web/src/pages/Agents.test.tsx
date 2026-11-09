import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { ChatAgent, Role } from '../api'
import { asRole } from '../test/access'
import { mockFetch, type Reply } from '../test/mockFetch'
import Agents from './Agents'

const BUILT_IN: ChatAgent[] = [
  {
    name: 'auto',
    label: 'Auto',
    description: 'Picks for you',
    aliases: ['ask'],
    custom: false,
  },
  {
    name: 'qa',
    label: 'QA',
    description: 'Test scenarios',
    aliases: [],
    custom: false,
  },
]
const SECURITY: ChatAgent = {
  name: 'security',
  label: 'Security',
  description: 'Who may do what',
  aliases: [],
  custom: true,
  route: 'code',
  focus: 'Name the guard.',
  note_types: ['known_issue'],
  created_by: 'me',
  updated_at: '2026-11-08T10:00:00Z',
}

function renderAs(role: Role, routes: Record<string, Reply>) {
  const fetch = mockFetch({
    'GET /api/chat/agents': { body: BUILT_IN },
    ...routes,
  })
  render(
    asRole(
      role,
      <MemoryRouter initialEntries={['/w/default/agents']}>
        <Routes>
          <Route path="/w/:workspace/agents" element={<Agents />} />
        </Routes>
      </MemoryRouter>,
    ),
  )
  return fetch
}

const sent = (fetch: ReturnType<typeof mockFetch>, method: string) =>
  fetch.mock.calls
    .filter(([, init]) => init?.method === method)
    .map(([url, init]) => [url, init?.body ? JSON.parse(init.body as string) : null])

describe('Agents', () => {
  it('is for admins only', () => {
    const fetch = renderAs('maintainer', {})
    expect(screen.getByText('Only an admin of default manages its agents.')).toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('adds an agent with its sources, note types and focus', async () => {
    const fetch = renderAs('admin', {
      'POST /api/chat/agents': { status: 201, body: SECURITY },
    })
    const builtIn = await screen.findByRole('list', {
      name: 'Built-in agents',
    })
    expect(within(builtIn).getByText('@qa')).toBeInTheDocument()
    expect(screen.getByText('No custom agents yet.')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'New agent' }))
    const form = screen.getByRole('form', { name: 'New agent' })
    const save = within(form).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    await userEvent.type(within(form).getByLabelText('Name (@name)'), 'Security')
    await userEvent.type(within(form).getByLabelText('Label'), 'Security')
    await userEvent.type(within(form).getByLabelText('Description'), 'Who may do what')
    await userEvent.selectOptions(within(form).getByLabelText('Sources'), 'agent')
    await userEvent.click(within(form).getByRole('checkbox', { name: 'Known issue' }))
    await userEvent.type(within(form).getByLabelText('Focus'), 'Name the guard.')
    await userEvent.click(save)

    await waitFor(() =>
      expect(sent(fetch, 'POST')).toEqual([
        [
          '/api/chat/agents',
          {
            name: 'security', // lowercased
            label: 'Security',
            description: 'Who may do what',
            route: 'agent',
            focus: 'Name the guard.',
            note_types: ['known_issue'],
          },
        ],
      ]),
    )
    // Saved: the form closes and the list reloads.
    await waitFor(() => expect(screen.queryByRole('form', { name: 'New agent' })).toBeNull())
  })

  it('shows a refusal and keeps the form', async () => {
    renderAs('admin', {
      'POST /api/chat/agents': {
        status: 422,
        body: { detail: '@qa is a built-in agent' },
      },
    })
    await userEvent.click(await screen.findByRole('button', { name: 'New agent' }))
    const form = screen.getByRole('form', { name: 'New agent' })
    await userEvent.type(within(form).getByLabelText('Name (@name)'), 'qa')
    await userEvent.type(within(form).getByLabelText('Label'), 'QA 2')
    await userEvent.click(within(form).getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('@qa is a built-in agent')).toBeInTheDocument()
    expect(screen.getByRole('form', { name: 'New agent' })).toBeInTheDocument()
  })

  it('edits a custom agent without its name, and deletes it after asking', async () => {
    const fetch = renderAs('admin', {
      'GET /api/chat/agents': { body: [...BUILT_IN, SECURITY] },
      'PATCH /api/chat/agents/security': {
        body: { ...SECURITY, focus: 'Name it.' },
      },
      'DELETE /api/chat/agents/security': { status: 204, body: null },
    })
    const custom = await screen.findByRole('list', { name: 'Custom agents' })
    expect(within(custom).getByText('Leans on: Known issue')).toBeInTheDocument()

    await userEvent.click(within(custom).getByRole('button', { name: 'Edit' }))
    const form = screen.getByRole('form', { name: 'Edit @security' })
    expect(within(form).getByLabelText('Name (@name)')).toBeDisabled()
    const focus = within(form).getByLabelText('Focus')
    await userEvent.clear(focus)
    await userEvent.type(focus, 'Name it.')
    await userEvent.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(sent(fetch, 'PATCH')).toEqual([
        [
          '/api/chat/agents/security',
          {
            label: 'Security',
            description: 'Who may do what',
            route: 'code',
            focus: 'Name it.',
            note_types: ['known_issue'],
          },
        ],
      ]),
    )

    await userEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(screen.getByText(/Its chats move to Auto/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Yes' }))
    await waitFor(() =>
      expect(sent(fetch, 'DELETE')).toEqual([['/api/chat/agents/security', null]]),
    )
  })
})

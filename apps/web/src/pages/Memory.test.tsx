import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { MemoryRow, Role } from '../api'
import { asRole } from '../test/access'
import { mockFetch, type Reply } from '../test/mockFetch'
import Memory from './Memory'

const memory = (id: number, overrides: Partial<MemoryRow> = {}): MemoryRow => ({
  id,
  text: `Memory ${id} text.`,
  created_at: '2026-11-07T10:00:00Z',
  api_key: 'laptop-a',
  embed_model: 'voyage-4',
  stale_model: false,
  ...overrides,
})

const ROWS = [
  memory(3, { api_key: 'laptop-b', text: 'Deploys freeze on Friday.' }),
  memory(2, { text: 'Logout fires twice when the app is killed.', stale_model: true }),
  memory(1, { text: 'Staging DB is db-stg-2.' }),
]

function renderAs(role: Role, routes: Record<string, Reply> = {}, url = '/memory') {
  const fetch = mockFetch({ 'GET /api/memories': { body: ROWS }, ...routes })
  render(
    asRole(
      role,
      <MemoryRouter initialEntries={[url]}>
        <Memory />
      </MemoryRouter>,
      'laptop-a',
    ),
  )
  return fetch
}

const item = async (text: string) => (await screen.findByText(text)).closest('li')!
const calls = (fetch: ReturnType<typeof mockFetch>) =>
  fetch.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${url}`)

describe('Memory', () => {
  it('lists every key for a maintainer, with the key filter and an old-model mark', async () => {
    const fetch = renderAs('maintainer', {
      'GET /api/memories?key=laptop-b': { body: [ROWS[0]] },
    })
    const theirs = await item('Deploys freeze on Friday.')
    expect(within(theirs).getByText('laptop-b')).toBeInTheDocument()
    const stale = await item('Logout fires twice when the app is killed.')
    expect(within(stale).getByText('older model')).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText('Key'), 'laptop-b')
    await waitFor(() => expect(calls(fetch)).toContain('GET /api/memories?key=laptop-b'))
    await waitFor(() => expect(screen.queryByText('Staging DB is db-stg-2.')).toBeNull())
  })

  it('shows a contributor only its own, without keys or the key filter', async () => {
    renderAs('contributor', { 'GET /api/memories': { body: ROWS.slice(1) } })
    expect(await screen.findByText('Staging DB is db-stg-2.')).toBeInTheDocument()
    expect(screen.queryByLabelText('Key')).toBeNull()
    expect(screen.queryByText('laptop-a')).toBeNull()
  })

  it('searches by words, and by meaning with a score', async () => {
    const fetch = renderAs('maintainer', {
      'GET /api/memories?q=staging': { body: [ROWS[2]] },
      'GET /api/memories?q=staging&mode=semantic': { body: [{ ...ROWS[2], score: 0.83 }] },
    })
    await screen.findByText('Deploys freeze on Friday.')

    await userEvent.click(screen.getByRole('button', { name: 'Meaning' }))
    expect(screen.getByText(/Type what an agent would search for/)).toBeInTheDocument()
    expect(calls(fetch)).not.toContain('GET /api/memories?mode=semantic') // no search without q

    await userEvent.click(screen.getByRole('button', { name: 'Words' }))
    await userEvent.type(screen.getByLabelText('Search memories'), 'staging')
    await waitFor(() => expect(calls(fetch)).toContain('GET /api/memories?q=staging'))
    await userEvent.click(screen.getByRole('button', { name: 'Meaning' }))
    expect(await screen.findByText('score 0.83')).toBeInTheDocument()
  })

  it('opens a memory with the notes made from it', async () => {
    renderAs('maintainer', {
      'GET /api/memories/1': {
        body: {
          ...ROWS[2],
          notes: [
            { id: 41, note_type: 'business_rule', status: 'proposed', target_ref: 'Staging' },
          ],
        },
      },
    })
    const row = await item('Staging DB is db-stg-2.')
    await userEvent.click(within(row).getByRole('button', { name: 'Open' }))
    expect(await within(row).findByRole('link', { name: 'Note 41' })).toHaveAttribute(
      'href',
      '/knowledge?tab=notes&id=41',
    )
    expect(within(row).getByText(/Embedded with voyage-4/)).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Less' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('deletes after a confirmation and reloads', async () => {
    // 204 in the API; mockFetch always sends a body, which a 204 Response cannot carry.
    const fetch = renderAs('maintainer', { 'DELETE /api/memories/3': { body: null } })
    const row = await item('Deploys freeze on Friday.')
    await userEvent.click(within(row).getByRole('button', { name: 'Delete' }))
    expect(calls(fetch)).not.toContain('DELETE /api/memories/3')
    await userEvent.click(within(row).getByRole('button', { name: 'Yes' }))
    await waitFor(() => expect(calls(fetch)).toContain('DELETE /api/memories/3'))
    await waitFor(() =>
      expect(calls(fetch).filter((c) => c === 'GET /api/memories')).toHaveLength(2),
    )
  })

  it('makes a proposal from a memory, its text filled in', async () => {
    const fetch = renderAs('maintainer', {
      'GET /api/targets?q=staging': {
        body: [
          {
            kind: 'entity',
            target_kind: 'entity',
            target_ref: 'Staging',
            repo: null,
            repo_name: null,
            label: 'Staging',
            detail: 'concept',
            summary: null,
          },
        ],
      },
      'POST /api/memories/1/note': { status: 201, body: { id: 41 } },
      'GET /api/memories/1': { body: { ...ROWS[2], notes: [] } },
    })
    const row = await item('Staging DB is db-stg-2.')
    await userEvent.click(within(row).getByRole('button', { name: 'Make a note' }))
    const form = within(row).getByRole('form', { name: 'Note from memory #1' })
    expect(within(form).queryByText('More options')).toBeNull() // no owner or re-check date
    await userEvent.type(within(form).getByLabelText('About what?'), 'staging')
    const entities = await within(form).findByRole('group', { name: 'Entities' })
    await userEvent.click(within(entities).getByRole('button', { name: /Staging/ }))
    expect(within(form).getByDisplayValue('Staging DB is db-stg-2.')).toBeInTheDocument()
    await userEvent.click(within(form).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(calls(fetch)).toContain('POST /api/memories/1/note'))
    const post = fetch.mock.calls.find(([url]) => url === '/api/memories/1/note')!
    expect(JSON.parse(String(post[1]?.body))).toMatchObject({
      target_kind: 'entity',
      target_ref: 'Staging',
      text: 'Staging DB is db-stg-2.',
    })
  })

  it('says what is missing when there is nothing yet', async () => {
    renderAs('contributor', { 'GET /api/memories': { body: [] } })
    expect(await screen.findByText(/No memories yet/)).toBeInTheDocument()
  })

  it('tells a viewer it needs the contributor role, without asking the API', () => {
    const fetch = renderAs('viewer')
    expect(screen.getByText(/need the contributor role/)).toBeInTheDocument()
    expect(calls(fetch)).toEqual([])
  })
})

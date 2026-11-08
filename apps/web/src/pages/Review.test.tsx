import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { Annotation, ReviewItem, Role } from '../api'
import { asRole } from '../test/access'
import { mockFetch, type Reply } from '../test/mockFetch'
import Review from './Review'

function note(id: number, overrides: Partial<Annotation> = {}): Annotation {
  return {
    id,
    target_kind: 'symbol',
    target_ref: 'src/auth/auth.service.ts#AuthService.logout',
    repo_id: 1,
    scope: 'deps',
    text: `Note ${id} text.`,
    author: 'web',
    by: 'Alice',
    owner: 'web',
    status: 'proposed',
    reason: null,
    conflict: false,
    target_hash: 'h',
    deps_hash: 'd',
    candidates: [],
    verified_by: null,
    verified_at: null,
    verify_by: null,
    created_at: '2026-11-02T09:00:00Z',
    updated_at: '2026-11-02T09:00:00Z',
    via: 'api',
    question: null,
    memory_id: null,
    note_type: 'expected_behavior',
    summary_kind: null,
    summary_text: null,
    ...overrides,
  }
}

const item = (reason: ReviewItem['reason'], n: Annotation, extra = {}): ReviewItem => ({
  reason,
  note: n,
  repo_name: 'org-app',
  summary_then: null,
  summary_now: 'Ends the session.',
  summary_now_kind: 'flow',
  ...extra,
})

const QUEUE: ReviewItem[] = [
  item('proposed', note(1)),
  item('needs_verification', note(2, { status: 'needs_verification', reason: 'hash' }), {
    summary_then: 'Ends the session and the token.',
  }),
  item(
    'orphaned',
    note(3, {
      status: 'orphaned',
      candidates: [
        { id: 'src/a.ts#Auth.signOut', qualified_name: 'Auth.signOut', file: 'src/a.ts' },
      ],
    }),
  ),
  item('conflict', note(4, { status: 'verified', conflict: true })),
]

function renderAs(role: Role, routes: Record<string, Reply> = {}, name = 'me', url = '/review') {
  const fetch = mockFetch({ 'GET /api/review': { body: QUEUE }, ...routes })
  render(
    asRole(
      role,
      <MemoryRouter initialEntries={[url]}>
        <Review />
      </MemoryRouter>,
      name,
    ),
  )
  return fetch
}

const card = (text: string) => screen.getByText(text).closest('li')!

const patches = (fetch: ReturnType<typeof mockFetch>) =>
  fetch.mock.calls
    .filter(([, init]) => init?.method === 'PATCH')
    .map(([url, init]) => [url, JSON.parse(String(init?.body))])

describe('Review inbox', () => {
  it('lists the waiting notes with their context and filters by reason', async () => {
    renderAs('maintainer')
    expect(await screen.findByText('Note 1 text.')).toBeInTheDocument()
    expect(within(card('Note 1 text.')).getByText('Ends the session.')).toBeInTheDocument()
    const flagged = card('Note 2 text.')
    expect(within(flagged).getByText('Ends the session and the token.')).toBeInTheDocument()
    expect(within(flagged).getByRole('button', { name: 'Still right' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /All\s*4/ })).toHaveAttribute('aria-pressed', 'true')

    await userEvent.click(screen.getByRole('button', { name: /Conflicts\s*1/ }))
    expect(screen.queryByText('Note 1 text.')).toBeNull()
    expect(screen.getByText('Note 4 text.')).toBeInTheDocument()
  })

  it('opens one filter from ?show= (the Dashboard links)', async () => {
    renderAs('maintainer', {}, 'me', '/review?show=orphaned')
    expect(await screen.findByText('Note 3 text.')).toBeInTheDocument()
    expect(screen.queryByText('Note 1 text.')).toBeNull()
    expect(screen.getByRole('button', { name: /Orphaned\s*1/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('decides in place and reloads', async () => {
    const fetch = renderAs('maintainer', {
      'PATCH /api/annotations/1': { body: note(1, { status: 'verified' }) },
      'PATCH /api/annotations/3': { body: note(3) },
      'PATCH /api/annotations/4': { body: note(4) },
    })
    await screen.findByText('Note 1 text.')
    await userEvent.click(within(card('Note 1 text.')).getByRole('button', { name: 'Verify' }))
    await userEvent.click(within(card('Note 3 text.')).getByRole('button', { name: 'Relink' }))
    await userEvent.click(
      within(card('Note 4 text.')).getByRole('button', { name: 'Code is right after all' }),
    )

    await waitFor(() =>
      expect(patches(fetch)).toEqual([
        ['/api/annotations/1', { status: 'verified' }],
        ['/api/annotations/3', { target_ref: 'src/a.ts#Auth.signOut' }],
        ['/api/annotations/4', { conflict: false }],
      ]),
    )
    expect(fetch.mock.calls.filter(([url]) => url === '/api/review').length).toBeGreaterThan(1)
  })

  it('shows a contributor the queue without the decisions', async () => {
    renderAs('contributor')
    await screen.findByText('Note 1 text.')
    expect(screen.queryByRole('button', { name: 'Verify' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Still right' })).toBeNull()
    expect(within(card('Note 1 text.')).getByText('A maintainer decides.')).toBeInTheDocument()
  })

  it('keeps a maintainer off its own agent proposal', async () => {
    mockFetch({
      'GET /api/review': { body: [item('proposed', note(9, { via: 'mcp', author: 'me' }))] },
    })
    render(
      asRole(
        'maintainer',
        <MemoryRouter>
          <Review />
        </MemoryRouter>,
        'me',
      ),
    )
    const own = (await screen.findByText('Note 9 text.')).closest('li')!
    expect(within(own).queryByRole('button', { name: 'Verify' })).toBeNull()
    expect(within(own).getByText(/another maintainer decides/)).toBeInTheDocument()
  })

  it('says when nothing waits', async () => {
    mockFetch({ 'GET /api/review': { body: [] } })
    render(
      asRole(
        'maintainer',
        <MemoryRouter>
          <Review />
        </MemoryRouter>,
      ),
    )
    expect(await screen.findByText('Nothing waits for review.')).toBeInTheDocument()
  })
})

describe('Review bulk decisions (A3)', () => {
  it('verifies the picked proposals and rejects the rest, reporting each refusal', async () => {
    const fetch = mockFetch({
      'GET /api/review': {
        body: [
          item('proposed', note(1)),
          item('proposed', note(5)),
          item('conflict', note(4, { status: 'verified', conflict: true })),
          item('needs_verification', note(2, { status: 'needs_verification' })),
        ],
      },
      'PATCH /api/annotations/1': { body: note(1) },
      'PATCH /api/annotations/5': {
        status: 403,
        body: { detail: 'Needs the maintainer role' },
      },
    })
    render(
      asRole(
        'maintainer',
        <MemoryRouter>
          <Review />
        </MemoryRouter>,
      ),
    )
    await screen.findByText('Note 1 text.')
    // A re-check is decided on its own, with then vs now.
    expect(screen.queryByRole('checkbox', { name: 'Select note 2' })).toBeNull()

    await userEvent.click(screen.getByRole('checkbox', { name: 'Select note 1' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select note 5' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select note 4' }))
    const bar = screen.getByRole('group', { name: 'Selected notes' })
    expect(bar).toHaveTextContent('3 selected')
    await userEvent.click(within(bar).getByRole('button', { name: 'Verify 2' }))

    await waitFor(() =>
      expect(patches(fetch)).toEqual([
        ['/api/annotations/1', { status: 'verified' }],
        ['/api/annotations/5', { status: 'verified' }],
      ]),
    )
    expect(await screen.findByText('note#5: Needs the maintainer role')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Selected notes' })).toBeNull()
  })

  it('offers no checkboxes to a contributor', async () => {
    renderAs('contributor')
    await screen.findByText('Note 1 text.')
    expect(screen.queryByRole('checkbox')).toBeNull()
  })
})

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { Annotation, Role } from '../api'
import { asRole } from '../test/access'
import { mockFetch } from '../test/mockFetch'
import Annotations from './Annotations'

function note(id: number, overrides: Partial<Annotation> = {}): Annotation {
  return {
    id,
    target_kind: 'symbol',
    target_ref: 'src/auth/auth.service.ts#AuthService.logout',
    repo_id: 1,
    scope: 'deps',
    text: 'Logout also revokes the refresh token.',
    author: 'web',
    by: 'Alice',
    owner: 'laptop-a',
    status: 'needs_verification',
    reason: 'deps',
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
    note_type: 'technical',
    summary_kind: null,
    summary_text: null,
    ...overrides,
  }
}

const REPOS = [
  {
    id: 1,
    url: 'https://github.com/org/app',
    ref: null,
    name: 'org-app',
    indexed_sha: 'a'.repeat(40),
    indexed_at: null,
    webhook: false,
  },
]

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/annotations" element={<Annotations />} />
      </Routes>
    </MemoryRouter>,
  )
}

function bodyOf(fetch: ReturnType<typeof mockFetch>, key: string) {
  const call = fetch.mock.calls.find(([url, init]) => `${init?.method ?? 'GET'} ${url}` === key)
  return call && JSON.parse(String(call[1]?.body))
}

describe('Annotations', () => {
  it('lists notes with target, status and reason, and filters by status', async () => {
    const fetch = mockFetch({
      'GET /api/annotations': { body: [note(3)] },
      'GET /api/annotations?status=orphaned': { body: [] },
      'GET /api/repos': { body: REPOS },
    })

    renderAt('/annotations')

    expect(await screen.findByText('Logout also revokes the refresh token.')).toBeInTheDocument()
    expect(await screen.findByText(/org-app · AuthService\.logout/)).toBeInTheDocument()
    const table = within(screen.getByRole('table'))
    expect(table.getByText('needs verification')).toHaveClass('status-needs_verification')
    expect(table.getByText('(deps)')).toBeInTheDocument()
    expect(table.getByText('Technical')).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText('Status'), 'orphaned')

    expect(await screen.findByText('No annotations.')).toBeInTheDocument()
    expect(fetch.mock.calls.map(([url]) => url)).toContain('/api/annotations?status=orphaned')
  })

  it('shows who proposed an MCP note, its question and its memory', async () => {
    mockFetch({
      'GET /api/annotations': {
        body: [
          note(4, {
            status: 'proposed',
            reason: null,
            via: 'mcp',
            author: 'laptop-a',
            question: 'Does logout revoke the refresh token?',
            memory_id: 12,
          }),
          note(5, { text: 'A plain note.' }),
        ],
      },
      'GET /api/repos': { body: REPOS },
    })

    renderAt('/annotations')

    expect(await screen.findByText('via MCP by laptop-a')).toBeInTheDocument()
    expect(screen.getByText('Question: Does logout revoke the refresh token?')).toBeInTheDocument()
    expect(screen.getByText('from memory #12')).toBeInTheDocument()
    expect(screen.getAllByText(/via MCP/)).toHaveLength(1) // not on the plain note
  })

  it('creates a note on a feature found by its route, without knowing the symbol', async () => {
    const fetch = mockFetch({
      'GET /api/annotations': { body: [] },
      'GET /api/repos': { body: REPOS },
      'GET /api/targets?q=logout': {
        body: [
          {
            kind: 'feature',
            target_kind: 'symbol',
            target_ref: 'src/a.ts#AuthController.logout',
            repo: 1,
            repo_name: 'org-app',
            label: 'POST /auth/logout',
            detail: 'AuthController.logout · src/a.ts:3',
            summary: 'Ends the session and revokes the refresh token.',
          },
          {
            kind: 'entity',
            target_kind: 'entity',
            target_ref: 'Logout',
            repo: null,
            repo_name: null,
            label: 'Logout',
            detail: 'concept',
            summary: null,
          },
        ],
      },
      'POST /api/annotations': { status: 201, body: note(4, { status: 'verified' }) },
    })

    renderAt('/annotations')
    await screen.findByText('No annotations.')
    await userEvent.click(screen.getByText('Add a note'))
    await userEvent.type(screen.getByLabelText('About what?'), 'logout')
    const features = await screen.findByRole('group', { name: 'Features' })
    expect(screen.getByRole('group', { name: 'Entities' })).toHaveTextContent('Logout')
    await userEvent.click(within(features).getByRole('button', { name: /POST \/auth\/logout/ }))
    expect(screen.getByText('Ends the session and revokes the refresh token.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('radio', { name: /Technical/ }))
    await userEvent.type(screen.getByLabelText('What should a developer know?'), 'Revokes tokens.')
    await userEvent.type(screen.getByLabelText('Your name'), 'Alice')
    await userEvent.click(screen.getByText('More options'))
    await userEvent.selectOptions(screen.getByLabelText('Check it again'), 'In 3 months')
    await userEvent.click(screen.getByRole('radio', { name: 'only this code changes' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(bodyOf(fetch, 'POST /api/annotations')).toBeDefined())
    const { verify_by, ...rest } = bodyOf(fetch, 'POST /api/annotations')
    expect(rest).toEqual({
      target_kind: 'symbol',
      target_ref: 'src/a.ts#AuthController.logout',
      text: 'Revokes tokens.',
      note_type: 'technical',
      repo: 1,
      scope: 'body',
      by: 'Alice',
    })
    const months = (new Date(verify_by).getTime() - Date.now()) / (30 * 24 * 3600 * 1000)
    expect(months).toBeGreaterThan(2.5)
    expect(months).toBeLessThan(3.2)
    expect(screen.getByLabelText('About what?')).toHaveValue('') // ready for the next note
  })

  it('shows the notes already on the target, flags a look-alike and previews the citation', async () => {
    mockFetch({
      'GET /api/annotations': { body: [] },
      'GET /api/repos': { body: [] },
      'GET /api/annotations?target_kind=entity&target_ref=Mira+Okafor': {
        body: [note(8, { target_kind: 'entity', text: 'Mira Okafor leads the harbor study.' })],
      },
    })
    renderAt('/annotations?target_kind=entity&target_ref=Mira%20Okafor')

    const here = await screen.findByLabelText('Notes already here')
    expect(here).toHaveTextContent('note#8 Mira Okafor leads the harbor study.')
    await userEvent.type(
      screen.getByLabelText('Which rule must this feature follow?'),
      'Mira Okafor leads the harbor study',
    )
    expect(within(here).getByText('Looks the same:')).toBeInTheDocument()
    const preview = screen.getByLabelText('How answers cite it')
    expect(preview).toHaveTextContent('note#… [verified by you')
    expect(preview).toHaveTextContent('"Mira Okafor leads the harbor study"')
  })

  it('takes the target from a link', async () => {
    mockFetch({ 'GET /api/annotations': { body: [] }, 'GET /api/repos': { body: [] } })

    renderAt('/annotations?target_kind=entity&target_ref=Mira%20Okafor')

    const form = await screen.findByRole('form', { name: 'New note' })
    expect(within(form).getByText('Mira Okafor')).toBeInTheDocument()
    expect(within(form).queryByPlaceholderText(/A route, a name/)).toBeNull() // no search
    expect(within(form).getByRole('button', { name: 'Change' })).toBeInTheDocument()
  })

  it('shows the API detail when verifying is refused', async () => {
    const fetch = mockFetch({
      'GET /api/annotations': { body: [note(3)] },
      'GET /api/repos': { body: REPOS },
      'PATCH /api/annotations/3': {
        status: 403,
        body: { detail: 'Only the owner (laptop-a) or a key in ANNOTATION_VERIFIERS may verify' },
      },
    })

    renderAt('/annotations')
    await userEvent.click(await screen.findByRole('button', { name: 'Verify' }))

    expect(await screen.findByText(/Only the owner \(laptop-a\)/)).toBeInTheDocument()
    expect(bodyOf(fetch, 'PATCH /api/annotations/3')).toEqual({ status: 'verified' })
  })

  it('deletes a note for good after a confirmation', async () => {
    const routes = {
      'GET /api/annotations': { body: [note(3), note(4, { text: 'Kept note.' })] },
      'GET /api/repos': { body: REPOS },
      'DELETE /api/annotations/3': { body: null },
    }
    const fetch = mockFetch(routes)

    renderAt('/annotations')
    const row = (await screen.findByText('Logout also revokes the refresh token.')).closest('tr')!
    await userEvent.click(within(row).getByRole('button', { name: 'Delete' }))
    expect(within(row).getByText('Delete for good?')).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument()
    routes['GET /api/annotations'] = { body: [note(4, { text: 'Kept note.' })] }
    await userEvent.click(within(row).getByRole('button', { name: 'Yes' }))

    expect(await screen.findByText('Kept note.')).toBeInTheDocument()
    expect(screen.queryByText('Logout also revokes the refresh token.')).not.toBeInTheDocument()
    expect(fetch.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(true)
  })

  it('relinks an orphaned note to a candidate and shows its history', async () => {
    const orphan = note(5, {
      status: 'orphaned',
      reason: 'orphan',
      candidates: [{ id: 'src/b.ts#Auth.logout', qualified_name: 'Auth.logout', file: 'src/b.ts' }],
    })
    const fetch = mockFetch({
      'GET /api/annotations': { body: [orphan] },
      'GET /api/repos': { body: REPOS },
      'PATCH /api/annotations/5': { body: { ...orphan, status: 'needs_verification' } },
      'GET /api/annotations/5': {
        body: {
          ...orphan,
          events: [
            {
              id: 1,
              annotation_id: 5,
              actor: 'worker',
              action: 'orphaned',
              old: { status: 'verified' },
              new: { status: 'orphaned', candidates: [] },
              at: '2026-11-03T01:00:00Z',
            },
          ],
        },
      },
    })

    renderAt('/annotations')
    const select = await screen.findByLabelText(`Relink ${orphan.target_ref}`)
    expect(within(select).getByRole('option')).toHaveTextContent('Auth.logout (src/b.ts)')
    await userEvent.click(screen.getByRole('button', { name: 'Relink' }))
    await userEvent.click(screen.getByRole('button', { name: 'History' }))

    expect(await screen.findByText(/worker · orphaned · status, candidates/)).toBeInTheDocument()
    expect(bodyOf(fetch, 'PATCH /api/annotations/5')).toEqual({
      target_ref: 'src/b.ts#Auth.logout',
    })
  })
})

describe('Annotations by role', () => {
  function renderAs(role: Role, name = 'me') {
    render(
      asRole(
        role,
        <MemoryRouter initialEntries={['/annotations']}>
          <Routes>
            <Route path="/annotations" element={<Annotations />} />
          </Routes>
        </MemoryRouter>,
        name,
      ),
    )
  }

  const ROUTES = (notes: Annotation[]) => ({
    'GET /api/annotations': { body: notes },
    'GET /api/repos': { body: REPOS },
  })

  it('shows a viewer no actions but the history', async () => {
    mockFetch(ROUTES([note(1, { author: 'me' })]))
    renderAs('viewer')
    const row = (await screen.findByText(/Logout also revokes/)).closest('tr')!
    expect(
      within(row)
        .getAllByRole('button')
        .map((b) => b.textContent),
    ).toEqual(['History'])
    expect(screen.queryByText('Add a note')).toBeNull()
    expect(screen.queryByRole('link', { name: /Graph editor/ })).toBeNull()
  })

  it('lets a contributor delete its own note but not verify', async () => {
    mockFetch(ROUTES([note(1, { author: 'me' }), note(2, { author: 'web' })]))
    renderAs('contributor')
    const [own, other] = await screen.findAllByText(/Logout also revokes/)
    const buttons = (el: HTMLElement) =>
      within(el.closest('tr')!)
        .getAllByRole('button')
        .map((b) => b.textContent)
    expect(buttons(own)).toEqual(['History', 'Delete'])
    expect(buttons(other)).toEqual(['History'])
    expect(screen.getByText('Add a note')).toBeInTheDocument()
  })

  it('keeps a maintainer from verifying its own agent proposal', async () => {
    mockFetch(
      ROUTES([
        note(1, { status: 'proposed', via: 'mcp', author: 'me' }),
        note(2, { status: 'proposed', via: 'mcp', author: 'laptop-b' }),
      ]),
    )
    renderAs('maintainer')
    const [own, other] = await screen.findAllByText(/Logout also revokes/)
    expect(within(own.closest('tr')!).queryByRole('button', { name: 'Verify' })).toBeNull()
    expect(within(other.closest('tr')!).getByRole('button', { name: 'Verify' })).toBeVisible()
  })
})

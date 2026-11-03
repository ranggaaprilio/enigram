import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { Annotation } from '../api'
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

    await userEvent.selectOptions(screen.getByLabelText('Status'), 'orphaned')

    expect(await screen.findByText('No annotations.')).toBeInTheDocument()
    expect(fetch.mock.calls.map(([url]) => url)).toContain('/api/annotations?status=orphaned')
  })

  it('creates a note on a symbol found through the picker', async () => {
    const fetch = mockFetch({
      'GET /api/annotations': { body: [] },
      'GET /api/repos': { body: REPOS },
      'GET /api/repos/1/symbols?q=logout': {
        body: [
          {
            id: 'src/a.ts#AuthService.logout',
            qualified_name: 'AuthService.logout',
            kind: 'method',
            file: 'src/a.ts',
            line_start: 3,
            line_end: 9,
          },
        ],
      },
      'POST /api/annotations': { status: 201, body: note(4, { status: 'verified' }) },
    })

    renderAt('/annotations')
    await screen.findByText('No annotations.')
    await userEvent.click(screen.getByText('New annotation'))
    await screen.findByRole('option', { name: 'org-app' })
    await userEvent.type(screen.getByLabelText('Symbol'), 'logout')
    await userEvent.click(screen.getByRole('button', { name: 'Find' }))
    await userEvent.click(await screen.findByRole('button', { name: 'AuthService.logout' }))
    await userEvent.type(screen.getByLabelText('Note'), 'Revokes tokens.')
    await userEvent.type(screen.getByLabelText('By'), 'Alice')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await screen.findByText('No annotations.')
    expect(bodyOf(fetch, 'POST /api/annotations')).toEqual({
      target_kind: 'symbol',
      target_ref: 'src/a.ts#AuthService.logout',
      text: 'Revokes tokens.',
      repo: 1,
      scope: 'deps',
      by: 'Alice',
    })
  })

  it('prefills the form from the URL', async () => {
    mockFetch({ 'GET /api/annotations': { body: [] }, 'GET /api/repos': { body: [] } })

    renderAt('/annotations?target_kind=entity&target_ref=Mira%20Okafor')

    expect(await screen.findByLabelText('Target')).toHaveValue('Mira Okafor')
    expect(screen.getByLabelText('Kind')).toHaveValue('entity')
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

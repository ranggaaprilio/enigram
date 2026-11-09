import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { Role } from '../api'
import { asRole } from '../test/access'
import { mockFetch, type Reply } from '../test/mockFetch'
import Members from './Members'

const MEMBERS = [
  { name: 'me', kind: 'key', role: 'admin', owner: false },
  { name: 'qa-team', kind: 'key', role: 'contributor', owner: false },
  {
    name: 'ann@example.com',
    kind: 'user',
    role: 'viewer',
    owner: false,
    display_name: 'Ann Lee',
    signed_in: true,
  },
  { name: 'bob@example.com', kind: 'user', role: 'contributor', owner: false, signed_in: false },
]

function renderAs(role: Role, routes: Record<string, Reply>, login = true) {
  const fetch = mockFetch({
    'GET /api/members': { body: MEMBERS },
    'GET /api/auth/config': { body: { login } },
    ...routes,
  })
  render(
    asRole(
      role,
      <MemoryRouter initialEntries={['/w/default/members']}>
        <Routes>
          <Route path="/w/:workspace/members" element={<Members />} />
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

describe('Members', () => {
  it('is for admins only', () => {
    const fetch = renderAs('maintainer', {})
    expect(screen.getByText('Only an admin of default manages its members.')).toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('lists the members and changes a role', async () => {
    const fetch = renderAs('admin', {
      'PUT /api/members/qa-team': { body: { name: 'qa-team', role: 'viewer' } },
    })
    expect(await screen.findByText('(you)')).toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText('Role of qa-team'), 'viewer')
    await waitFor(() =>
      expect(sent(fetch, 'PUT')).toEqual([['/api/members/qa-team', { role: 'viewer' }]]),
    )
  })

  it('makes a key and shows it once', async () => {
    const fetch = renderAs('admin', {
      'POST /api/keys': { status: 201, body: { name: 'pm', role: 'viewer', key: 'k-secret-1' } },
    })
    const form = await screen.findByRole('form', { name: 'Make a new key' })
    await userEvent.type(within(form).getByLabelText('Key name'), 'pm')
    await userEvent.click(within(form).getByRole('button', { name: 'Make key' }))

    expect(await screen.findByText('k-secret-1')).toBeInTheDocument()
    expect(sent(fetch, 'POST')).toEqual([['/api/keys', { name: 'pm', role: 'viewer' }]])
    await userEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByText('k-secret-1')).toBeNull()
  })

  it('asks before revoking and shows the refusal', async () => {
    const fetch = renderAs('admin', {
      'DELETE /api/keys/me': {
        status: 422,
        body: { detail: 'me is the last admin of default; make another member admin first' },
      },
    })
    const row = (await screen.findByText('(you)')).closest('tr')!
    await userEvent.click(within(row).getByRole('button', { name: 'Revoke key' }))
    await userEvent.click(within(row).getByRole('button', { name: "Revoke me's key" }))

    expect(await screen.findByText(/last admin of default/)).toBeInTheDocument()
    expect(sent(fetch, 'DELETE')).toEqual([['/api/keys/me', null]])
  })

  it('splits people from keys; a person has a status and no revoke', async () => {
    renderAs('admin', {})
    const people = await screen.findByRole('region', { name: 'People' })
    const keys = screen.getByRole('region', { name: 'API keys' })

    expect(within(people).getByText('Ann Lee')).toBeInTheDocument()
    expect(within(people).getByText('active')).toBeInTheDocument()
    expect(within(people).getByText('invited')).toBeInTheDocument()
    expect(within(people).queryByRole('button', { name: 'Revoke key' })).toBeNull()
    expect(within(keys).getByText('qa-team')).toBeInTheDocument()
    expect(within(keys).queryByText('bob@example.com')).toBeNull()
    expect(screen.queryByRole('form', { name: 'Add an existing key' })).toBeNull()
  })

  it('invites a person by email and gives the link to send', async () => {
    const fetch = renderAs('admin', {
      'POST /api/members': { status: 201, body: { name: 'cy@example.com', role: 'maintainer' } },
    })
    const form = await screen.findByRole('form', { name: 'Invite a person' })
    await userEvent.type(within(form).getByLabelText('Email'), 'cy@example.com')
    await userEvent.selectOptions(within(form).getByLabelText('Role'), 'maintainer')
    await userEvent.click(within(form).getByRole('button', { name: 'Invite' }))

    expect(await screen.findByText('Send cy@example.com this link')).toBeInTheDocument()
    expect(screen.getByLabelText('Workspace link')).toHaveTextContent(/\/w\/default$/)
    expect(sent(fetch, 'POST')).toEqual([
      ['/api/members', { email: 'cy@example.com', role: 'maintainer' }],
    ])
  })

  it('keeps the email when the API refuses the invite', async () => {
    renderAs('admin', {
      'POST /api/members': {
        status: 422,
        body: { detail: 'eve@other.com may not sign in here (OIDC_ALLOWED_DOMAINS)' },
      },
    })
    const form = await screen.findByRole('form', { name: 'Invite a person' })
    await userEvent.type(within(form).getByLabelText('Email'), 'eve@other.com')
    await userEvent.click(within(form).getByRole('button', { name: 'Invite' }))

    expect(await screen.findByText(/may not sign in here/)).toBeInTheDocument()
    expect(within(form).getByLabelText('Email')).toHaveValue('eve@other.com')
  })

  it('hides People without sign-in', async () => {
    renderAs('admin', {}, false)
    expect(await screen.findByText('qa-team')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'People' })).toBeNull()
    expect(screen.queryByRole('form', { name: 'Invite a person' })).toBeNull()
  })
})

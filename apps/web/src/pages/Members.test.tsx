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
]

function renderAs(role: Role, routes: Record<string, Reply>) {
  const fetch = mockFetch({ 'GET /api/members': { body: MEMBERS }, ...routes })
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

  it('keeps the form when the API refuses an existing key', async () => {
    renderAs('admin', {
      'PUT /api/members/nobody': { status: 404, body: { detail: "No principal 'nobody'" } },
    })
    const form = await screen.findByRole('form', { name: 'Add an existing key' })
    await userEvent.type(within(form).getByLabelText('Key name'), 'nobody')
    await userEvent.click(within(form).getByRole('button', { name: 'Add' }))

    expect(await screen.findByText("No principal 'nobody'")).toBeInTheDocument()
    expect(within(form).getByLabelText('Key name')).toHaveValue('nobody')
  })
})

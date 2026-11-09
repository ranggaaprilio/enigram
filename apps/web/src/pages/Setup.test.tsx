import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { accessOf, AccessProvider } from '../access'
import type { Role } from '../api'
import { mockFetch, type Reply } from '../test/mockFetch'
import Setup from './Setup'

const SETUP = {
  workspace: 'default',
  settings: [
    {
      group: 'Models',
      label: 'Chat model',
      env: 'LLM_MODEL',
      value: 'deepseek deepseek-flash',
      set: true,
      secret: false,
    },
    {
      group: 'Models',
      label: 'TypeSafe API key',
      env: 'TYPESAFE_API_KEY',
      value: null,
      set: false,
      secret: true,
    },
    {
      group: 'Repositories',
      label: 'Private repository key',
      env: 'REPO_CREDENTIALS_KEY',
      value: null,
      set: true,
      secret: true,
    },
  ],
}

const REPOS = [
  {
    id: 3,
    url: 'https://github.com/org/app',
    ref: 'main',
    name: 'org/app',
    indexed_sha: 'abc',
    indexed_at: null,
    webhook: false,
  },
]

// The page as a key with `role` in "default"; an owner is admin everywhere.
function renderAs(role: Role | 'owner', routes: Record<string, Reply> = {}) {
  const fetch = mockFetch({
    'GET /api/setup': { body: SETUP },
    'GET /api/repos': { body: REPOS },
    ...routes,
  })
  const me = {
    principal: { name: 'me', kind: 'key', owner: role === 'owner', default_workspace: 'default' },
    roles: { default: role === 'owner' ? 'admin' : role },
  }
  render(
    <AccessProvider value={accessOf(me, 'default')}>
      <MemoryRouter initialEntries={['/w/default/setup']}>
        <Routes>
          <Route path="/w/:workspace/setup" element={<Setup />} />
        </Routes>
      </MemoryRouter>
    </AccessProvider>,
  )
  return fetch
}

describe('Setup', () => {
  it('is for admins only', () => {
    const fetch = renderAs('maintainer')
    expect(screen.getByText('Only an admin of default sets it up.')).toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('shows an owner the server settings, secrets as set or not only', async () => {
    renderAs('owner')
    const table = await screen.findByRole('table', { name: 'Models' })
    expect(within(table).getByText('deepseek deepseek-flash')).toBeInTheDocument()
    expect(within(table).getByText('TYPESAFE_API_KEY')).toBeInTheDocument()
    expect(within(table).getByText('Not set')).toBeInTheDocument()
    const repos = screen.getByRole('table', { name: 'Repositories' })
    expect(within(repos).getByText('Set')).toBeInTheDocument()
  })

  it('keeps the server settings from an admin who is no owner', async () => {
    const fetch = renderAs('admin')
    expect(await screen.findByText('org/app')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Server settings' })).toBeNull()
    expect(fetch.mock.calls.some(([url]) => url === '/api/setup')).toBe(false)
  })

  it('builds the MCP command for this workspace from the API address', async () => {
    renderAs('admin')
    const command = await screen.findByLabelText('Claude Code command')
    expect(command).toHaveTextContent(
      'claude mcp add --transport http enigram-default http://localhost:8080/mcp/w/default',
    )
    const address = screen.getByLabelText('API address')
    await userEvent.clear(address)
    await userEvent.type(address, 'https://enigram.example.com/')
    expect(command).toHaveTextContent('https://enigram.example.com/mcp/w/default')

    // Pasted into a shell: anything but a plain http(s) address falls back to the default.
    await userEvent.type(address, '; rm -rf ~')
    expect(screen.getByRole('alert')).toHaveTextContent('Not an http(s) address')
    expect(command).toHaveTextContent('http://localhost:8080/mcp/w/default')
  })

  it('makes a webhook secret after a confirmation and shows it once with its URL', async () => {
    const fetch = renderAs('admin', {
      'POST /api/repos/3/secret': { body: { secret: 's3cr3t' } },
    })
    const row = (await screen.findByText('org/app')).closest('tr') as HTMLElement
    expect(within(row).getByText('Not set')).toBeInTheDocument()
    await userEvent.click(within(row).getByRole('button', { name: 'Make secret' }))
    const confirm = screen.getByRole('button', { name: 'Make secret for org/app' })
    fireEvent.click(confirm)
    fireEvent.click(confirm) // before the page renders again

    const shown = await screen.findByRole('status', { name: 'Webhook for org/app' })
    expect(within(shown).getByText('s3cr3t')).toBeInTheDocument()
    expect(within(shown).getByText('http://localhost:8080/repos/3/webhook')).toBeInTheDocument()
    await waitFor(() =>
      expect(fetch.mock.calls.filter(([url]) => url === '/api/repos').length).toBe(2),
    )
    // A double click rotates once: a second rotation would void the secret shown.
    expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    await userEvent.click(within(shown).getByRole('button', { name: 'Done' }))
    expect(screen.queryByText('s3cr3t')).toBeNull()
  })
})

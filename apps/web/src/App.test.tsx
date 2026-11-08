import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { setWorkspace } from './api'
import { mockFetch, type Reply } from './test/mockFetch'
import { WORKSPACE_RULE } from './workspace'

function Where() {
  const { pathname, search } = useLocation()
  return <output data-testid="where">{pathname + search}</output>
}

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <Where />
    </MemoryRouter>,
  )
}

const where = () => screen.getByTestId('where').textContent

const WORKSPACES = [
  { name: 'default', created_at: '2026-11-07T00:00:00Z' },
  { name: 'ws-a', created_at: '2026-11-07T00:00:00Z' },
]

const ROUTES: Record<string, Reply> = {
  'GET /api/health': { body: { status: 'ok', workspace: 'default' } },
  'GET /api/workspaces': { body: WORKSPACES },
  'GET /api/documents': { body: [] },
  'GET /api/repos': { body: [] },
}

const header = (init?: RequestInit) => new Headers(init?.headers).get('X-Enigram-Workspace')

// The workspace header of every call to `url`, in order.
function headersOf(fetch: ReturnType<typeof mockFetch>, url: string) {
  return fetch.mock.calls.filter(([u]) => u === url).map(([, init]) => header(init))
}

afterEach(() => {
  setWorkspace(null)
  document.documentElement.classList.remove('dark')
})

describe('App workspaces', () => {
  it('redirects / to the process workspace', async () => {
    mockFetch(ROUTES)
    renderAt('/')
    await waitFor(() => expect(where()).toBe('/w/default/documents'))
  })

  it('redirects old links into the process workspace with their query', async () => {
    mockFetch(ROUTES)
    renderAt('/code?repo=1&symbol=x')
    await waitFor(() => expect(where()).toBe('/w/default/code?repo=1&symbol=x'))
  })

  it('redirects an old trace link', async () => {
    mockFetch(ROUTES)
    renderAt('/trace/12')
    await waitFor(() => expect(where()).toBe('/w/default/trace/12'))
  })

  it('takes the process workspace from /health', async () => {
    mockFetch({
      ...ROUTES,
      'GET /api/health': { body: { status: 'ok', workspace: 'main' } },
      'GET /api/workspaces': { body: [{ name: 'main', created_at: '2026-11-07T00:00:00Z' }] },
    })
    renderAt('/')
    await waitFor(() => expect(where()).toBe('/w/main/documents'))
  })

  it("sends the URL's workspace and keeps it in links and the title", async () => {
    const fetch = mockFetch(ROUTES)
    renderAt('/w/ws-a/documents')
    await waitFor(() => expect(headersOf(fetch, '/api/documents')).toContain('ws-a'))
    expect(headersOf(fetch, '/api/documents').every((h) => h === 'ws-a')).toBe(true)
    expect(screen.getByRole('link', { name: 'Code' })).toHaveAttribute('href', '/w/ws-a/code')
    expect(document.title).toBe('ws-a · enigram')
  })

  it('switches to the same page in the other workspace', async () => {
    const fetch = mockFetch(ROUTES)
    renderAt('/w/ws-a/code?repo=1')
    const select = await screen.findByLabelText('Workspace')
    await waitFor(() => expect(headersOf(fetch, '/api/repos')).toEqual(['ws-a']))
    await userEvent.selectOptions(select, 'default')
    expect(where()).toBe('/w/default/code')
    await waitFor(() => expect(headersOf(fetch, '/api/repos').at(-1)).toBe('default'))
    expect(document.title).toBe('default · enigram')
  })

  it('checks the name, then creates the workspace and opens it', async () => {
    const fetch = mockFetch({
      ...ROUTES,
      'POST /api/workspaces': {
        status: 201,
        body: { name: 'whiteboard', created_at: '2026-11-07T00:00:00Z' },
      },
    })
    renderAt('/w/default/code')
    await userEvent.click(await screen.findByRole('button', { name: 'New workspace' }))
    const input = screen.getByLabelText('Workspace name')

    await userEvent.type(input, 'Bad_Name')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(screen.getByText(`Use ${WORKSPACE_RULE}.`)).toBeInTheDocument()
    expect(fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)

    await userEvent.clear(input)
    await userEvent.type(input, 'whiteboard')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(where()).toBe('/w/whiteboard/documents'))
    const post = fetch.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(post?.[1]?.body as string)).toEqual({ name: 'whiteboard' })
    expect(screen.getByLabelText('Workspace')).toHaveValue('whiteboard')
    await waitFor(() => expect(headersOf(fetch, '/api/documents')).toContain('whiteboard'))
  })

  it("shows the API's refusal", async () => {
    mockFetch({
      ...ROUTES,
      'POST /api/workspaces': { status: 409, body: { detail: "Workspace 'ws-a' already exists" } },
    })
    renderAt('/w/default/documents')
    await userEvent.click(await screen.findByRole('button', { name: 'New workspace' }))
    await userEvent.type(screen.getByLabelText('Workspace name'), 'ws-a')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText("Workspace 'ws-a' already exists")).toBeInTheDocument()
    expect(where()).toBe('/w/default/documents')
  })

  it('names an unknown workspace and sends no page request', async () => {
    const fetch = mockFetch(ROUTES)
    renderAt('/w/nope/documents')
    expect(await screen.findByText(/No workspace “nope”/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open default' })).toHaveAttribute(
      'href',
      '/w/default/documents',
    )
    expect(headersOf(fetch, '/api/documents')).toEqual([])
  })

  it('sends no header for a name off the rule', async () => {
    const fetch = mockFetch(ROUTES)
    renderAt('/w/a%20b/documents')
    expect(await screen.findByText(/No workspace/)).toBeInTheDocument()
    expect(headersOf(fetch, '/api/workspaces')).toEqual([null])
  })

  it('shows the API as unreachable and still renders the page', async () => {
    const fetch = mockFetch({ ...ROUTES, 'GET /api/workspaces': { status: 500, body: null } })
    renderAt('/w/ws-a/documents')
    expect(await screen.findByText('API unreachable')).toBeInTheDocument()
    await waitFor(() => expect(headersOf(fetch, '/api/documents')).toContain('ws-a'))
  })
})

describe('App frame', () => {
  it('reaches every page from the sidebar and marks the current one', async () => {
    mockFetch(ROUTES)
    renderAt('/w/ws-a/code')
    const nav = await screen.findByRole('navigation', { name: 'Pages' })
    const links = within(nav).getAllByRole('link')
    expect(links.map((l) => [l.textContent, l.getAttribute('href')])).toEqual([
      ['Ask', '/w/ws-a/ask'],
      ['Trace', '/w/ws-a/trace'],
      ['Documents', '/w/ws-a/documents'],
      ['Code', '/w/ws-a/code'],
      ['Annotations', '/w/ws-a/annotations'],
      ['Graph', '/w/ws-a/graph'],
      ['Members', '/w/ws-a/members'], // no /me here: the API alone decides
    ])
    expect(within(nav).getByRole('link', { name: 'Code' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('banner')).toHaveTextContent('ws-a / Code')
  })

  it('shows the key and its role, and keeps the theme picked', async () => {
    mockFetch({
      ...ROUTES,
      'GET /api/me': {
        body: {
          principal: { name: 'qa-team', kind: 'key', owner: false, default_workspace: 'ws-a' },
          roles: { 'ws-a': 'contributor' },
        },
      },
    })
    renderAt('/w/ws-a/documents')
    const menu = await screen.findByRole('button', { name: 'Account and theme' })
    await waitFor(() => expect(menu).toHaveTextContent('contributor · ws-a'))
    expect(menu).toHaveTextContent('qa-team')

    await userEvent.click(menu)
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Dark' }))
    expect(document.documentElement).toHaveClass('dark')
    expect(localStorage.getItem('enigram-theme')).toBe('dark')

    await userEvent.click(menu)
    await userEvent.click(await screen.findByRole('menuitem', { name: 'System' }))
    expect(document.documentElement).not.toHaveClass('dark') // the test system is light
    expect(localStorage.getItem('enigram-theme')).toBeNull()
  })

  it('names the open access mode when the API has no keys', async () => {
    mockFetch({ ...ROUTES, 'GET /api/me': { body: { principal: null, roles: {} } } })
    renderAt('/w/default/documents')
    const menu = await screen.findByRole('button', { name: 'Account and theme' })
    await waitFor(() => expect(menu).toHaveTextContent('admin · default'))
    expect(menu).toHaveTextContent('Local')
  })

  it('remembers a collapsed sidebar', async () => {
    mockFetch(ROUTES)
    renderAt('/w/default/documents')
    const bar = await screen.findByRole('banner')
    await userEvent.click(within(bar).getByRole('button', { name: 'Toggle Sidebar' }))
    expect(localStorage.getItem('enigram-sidebar')).toBe('closed')
  })
})

describe('App by role', () => {
  const as = (role: string, owner = false) => ({
    ...ROUTES,
    'GET /api/me': {
      body: {
        principal: { name: 'k', kind: 'key', owner, default_workspace: 'default' },
        roles: { default: role },
      },
    },
  })

  it('hides Members and New workspace from a viewer', async () => {
    mockFetch(as('viewer'))
    renderAt('/w/default/documents')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Account and theme' })).toHaveTextContent(
        'viewer · default',
      ),
    )
    expect(screen.queryByRole('link', { name: 'Members' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'New workspace' })).toBeNull()
  })

  it('shows an admin Members, and an owner New workspace', async () => {
    mockFetch(as('admin'))
    renderAt('/w/default/documents')
    expect(await screen.findByRole('link', { name: 'Members' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New workspace' })).toBeNull()
  })

  it('shows an owner New workspace', async () => {
    mockFetch(as('viewer', true))
    renderAt('/w/default/documents')
    expect(await screen.findByRole('button', { name: 'New workspace' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Members' })).toBeInTheDocument()
  })
})

describe('Sign-in (R4)', () => {
  const signedOut = {
    ...ROUTES,
    'GET /api/me': { status: 401, body: { detail: 'Missing or invalid API key' } },
    'GET /api/workspaces': { status: 401, body: { detail: 'Missing or invalid API key' } },
  }

  it('asks to sign in and comes back to the same page', async () => {
    mockFetch({ ...signedOut, 'GET /api/auth/config': { body: { login: true } } })
    renderAt('/w/default/code?repo=1')
    const link = await screen.findByRole('link', { name: 'Sign in' })
    expect(link).toHaveAttribute(
      'href',
      `/api/auth/login?next=${encodeURIComponent('/w/default/code?repo=1')}`,
    )
    expect(screen.queryByRole('navigation', { name: 'Pages' })).toBeNull()
  })

  it('shows why the last sign-in failed and drops it from the way back', async () => {
    mockFetch({ ...signedOut, 'GET /api/auth/config': { body: { login: true } } })
    renderAt('/w/default/documents?login_error=eve%40other.com+may+not+sign+in')
    expect(await screen.findByText('eve@other.com may not sign in')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute(
      'href',
      `/api/auth/login?next=${encodeURIComponent('/w/default/documents')}`,
    )
  })

  it('names the shared key when the API has no sign-in', async () => {
    mockFetch({ ...signedOut, 'GET /api/auth/config': { body: { login: false } } })
    renderAt('/w/default/documents')
    expect(await screen.findByText(/refused the web UI's key/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Sign in' })).toBeNull()
  })

  it('shows a signed-in user by name and signs out', async () => {
    const fetch = mockFetch({
      ...ROUTES,
      'GET /api/me': {
        body: {
          principal: {
            name: 'ann@example.com',
            kind: 'user',
            owner: false,
            default_workspace: 'default',
            display_name: 'Ann Lee',
          },
          roles: { default: 'viewer' },
        },
      },
      'POST /api/auth/logout': { status: 204, body: null },
    })
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    renderAt('/w/default/documents')
    const menu = await screen.findByRole('button', { name: 'Account and theme' })
    await waitFor(() => expect(menu).toHaveTextContent('Ann Lee'))

    await userEvent.click(menu)
    expect(screen.getByText('ann@example.com')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('menuitem', { name: 'Sign out' }))

    await waitFor(() => expect(assign).toHaveBeenCalledWith('/'))
    const [, init] = fetch.mock.calls.find(([url]) => url === '/api/auth/logout')!
    expect(new Headers(init?.headers).get('X-Requested-With')).toBe('enigram')
  })
})

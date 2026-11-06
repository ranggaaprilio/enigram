import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { Annotation, Feature, SymbolView } from '../api'
import { mockFetch } from '../test/mockFetch'
import Code from './Code'

const CONTROLLER = 'src/app.controller.ts#AppController.find'
const SERVICE = 'src/app.service.ts#AppService.find'

function note(id: number, overrides: Partial<Annotation> = {}): Annotation {
  return {
    id,
    target_kind: 'symbol',
    target_ref: CONTROLLER,
    repo_id: 1,
    scope: 'deps',
    text: 'Logout also revokes the refresh token.',
    author: 'web',
    by: 'Alice',
    owner: 'web',
    status: 'verified',
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

const feature = (changes: Partial<Feature> = {}): Feature => ({
  symbol_id: CONTROLLER,
  qualified_name: 'AppController.find',
  kind: 'http_route',
  route: 'GET /users/:id',
  framework: 'nest',
  file: 'src/app.controller.ts',
  line_start: 8,
  line_end: 11,
  notes: 1,
  ...changes,
})

const view: SymbolView = {
  symbol: {
    id: CONTROLLER,
    qualified_name: 'AppController.find',
    kind: 'method',
    file: 'src/app.controller.ts',
    line_start: 8,
    line_end: 11,
    parent: 'AppController',
    signature: null,
    exported: true,
    decorators: ['Get'],
    body: "@Get(':id')\nfind(@Param('id') id: string) {\n  return this.service.find(id)\n}",
  },
  location: 'src/app.controller.ts:8-11@aaaaaaaaaaaa',
  entrypoints: [{ kind: 'http_route', route: 'GET /users/:id', framework: 'nest' }],
  summary: { flow: '1. `AppService.find` loads the user.', symbol: null },
  steps: [
    {
      id: SERVICE,
      qualified_name: 'AppService.find',
      kind: 'method',
      file: 'src/app.service.ts',
      line_start: 3,
      line_end: 9,
      summary: 'Finds a user by id.',
      notes: 1,
    },
  ],
  external: [],
  notes: [note(3)],
}

const FEATURES = {
  features: [
    feature(),
    feature({
      symbol_id: 'src/main.ts#bootstrap',
      qualified_name: 'bootstrap',
      kind: 'bootstrap',
      route: 'bootstrap',
      notes: 0,
    }),
  ],
  total: 2,
}

const SYMBOL_URL = `/api/repos/1/symbols/${encodeURIComponent(CONTROLLER)}`
const SERVICE_URL = `/api/repos/1/symbols/${encodeURIComponent(SERVICE)}`
const AT_SYMBOL = `/code?repo=1&symbol=${encodeURIComponent(CONTROLLER)}`
const BASE = {
  'GET /api/repos': { body: REPOS },
  'GET /api/repos/1/features': { body: FEATURES },
}

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/code" element={<Code />} />
      </Routes>
    </MemoryRouter>,
  )
}

function bodyOf(fetch: ReturnType<typeof mockFetch>, key: string) {
  const call = fetch.mock.calls.find(([url, init]) => `${init?.method ?? 'GET'} ${url}` === key)
  return call && JSON.parse(String(call[1]?.body))
}

describe('Code', () => {
  it('lists the features of the first repository with their note counts, and filters on the server', async () => {
    mockFetch({
      ...BASE,
      'GET /api/repos/1/features?q=u': { body: { features: [feature()], total: 1 } },
    })

    renderAt('/code')

    expect(await screen.findByText('GET /users/:id')).toBeInTheDocument()
    expect(screen.getByText('bootstrap')).toBeInTheDocument()
    expect(screen.getByText('2 features')).toBeInTheDocument()
    expect(screen.getByText('Pick a feature to see what it does.')).toBeInTheDocument()

    // One character: every keystroke is a request, and every URL must be mocked.
    await userEvent.type(screen.getByLabelText('Filter features'), 'u')

    expect(await screen.findByText('1 feature')).toBeInTheDocument()
    expect(screen.queryByText('bootstrap')).toBeNull()
  })

  it('shows the summary, steps and notes of a feature, and posts a note on it', async () => {
    const fetch = mockFetch({
      ...BASE,
      [`GET ${SYMBOL_URL}`]: { body: view },
      'POST /api/annotations': { status: 201, body: note(4) },
    })

    renderAt('/code')
    await userEvent.click(await screen.findByRole('button', { name: /GET \/users\/:id/ }))

    expect(await screen.findByRole('heading', { name: 'AppController.find' })).toBeInTheDocument()
    expect(screen.getByText('loads the user.', { exact: false })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'AppService.find' })).toBeInTheDocument()
    expect(screen.getByText('Finds a user by id.')).toBeInTheDocument()
    expect(screen.getByText('Notes (1)')).toBeInTheDocument()
    expect(screen.getByText('Logout also revokes the refresh token.')).toBeInTheDocument()
    // The target is fixed: no symbol lookup, no target field.
    expect(screen.getByText('New note on AppController.find')).toBeInTheDocument()
    expect(screen.queryByLabelText('Symbol')).toBeNull()
    expect(screen.queryByLabelText('Target')).toBeNull()

    await userEvent.type(screen.getByLabelText('Note'), 'Only admins.')
    await userEvent.selectOptions(screen.getByLabelText('Type'), 'Business rule')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(fetch.mock.calls.filter(([url]) => url === SYMBOL_URL)).toHaveLength(2),
    )
    expect(bodyOf(fetch, 'POST /api/annotations')).toEqual({
      target_kind: 'symbol',
      target_ref: CONTROLLER,
      text: 'Only admins.',
      note_type: 'business_rule',
      repo: 1,
      scope: 'deps',
    })
  })

  it('opens a step as its own symbol', async () => {
    mockFetch({
      ...BASE,
      [`GET ${SYMBOL_URL}`]: { body: view },
      [`GET ${SERVICE_URL}`]: {
        body: {
          ...view,
          symbol: { ...view.symbol, id: SERVICE, qualified_name: 'AppService.find' },
          entrypoints: [],
          summary: { flow: null, symbol: 'Finds a user by id.' },
          steps: [],
          notes: [],
        },
      },
    })

    renderAt(AT_SYMBOL)
    await userEvent.click(await screen.findByRole('button', { name: 'AppService.find' }))

    expect(await screen.findByRole('heading', { name: 'AppService.find' })).toBeInTheDocument()
    expect(screen.getByText('Finds a user by id.')).toBeInTheDocument()
    expect(screen.getByText('No notes on this symbol yet.')).toBeInTheDocument()
    expect(screen.getByText(/No steps/)).toBeInTheDocument()
    expect(screen.getByText('New note on AppService.find')).toBeInTheDocument()
  })

  it('keeps the code collapsed until it is opened', async () => {
    mockFetch({ ...BASE, [`GET ${SYMBOL_URL}`]: { body: view } })

    renderAt(AT_SYMBOL)

    // jsdom loads no Tailwind, so check the state the `data-[state=closed]:hidden` class keys on.
    const code = await screen.findByText(/return this\.service\.find/)
    const region = code.closest('[data-slot="collapsible-content"]')
    expect(region).toHaveAttribute('data-state', 'closed')
    await userEvent.click(screen.getByRole('button', { name: 'Code (4 lines)' }))
    expect(region).toHaveAttribute('data-state', 'open')
  })

  it('says when the symbol is gone', async () => {
    mockFetch(BASE)

    renderAt(AT_SYMBOL)

    expect(await screen.findByText(/Symbol not found/)).toBeInTheDocument()
  })

  it('falls back to the first repository when the linked one is gone', async () => {
    const fetch = mockFetch(BASE)

    renderAt('/code?repo=9')

    expect(await screen.findByText('GET /users/:id')).toBeInTheDocument()
    expect(screen.getByLabelText('Repository')).toHaveValue('1')
    expect(fetch.mock.calls.map(([url]) => url)).not.toContain('/api/repos/9/features')
  })

  it('asks for a repository when none is indexed', async () => {
    const fetch = mockFetch({ 'GET /api/repos': { body: [] } })

    renderAt('/code')

    expect(await screen.findByText(/No repository indexed yet/)).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})

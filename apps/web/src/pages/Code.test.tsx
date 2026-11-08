import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Annotation, Feature, SymbolView } from '../api'
import { asRole } from '../test/access'
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
  reviewed: false,
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
  callers: [],
  notes: [note(3)],
  review: null,
}

const TREE = {
  files: [
    {
      path: 'src/app.controller.ts',
      symbols: [
        {
          id: 'src/app.controller.ts#AppController',
          qualified_name: 'AppController',
          kind: 'class',
          line_start: 3,
          line_end: 12,
          parent: null,
          notes: 0,
        },
        {
          id: CONTROLLER,
          qualified_name: 'AppController.find',
          kind: 'method',
          line_start: 8,
          line_end: 11,
          parent: 'AppController',
          notes: 1,
        },
      ],
    },
    {
      path: 'src/app.service.ts',
      symbols: [
        {
          id: SERVICE,
          qualified_name: 'AppService.find',
          kind: 'method',
          line_start: 3,
          line_end: 9,
          parent: 'AppService',
          notes: 2,
        },
      ],
    },
    { path: 'src/types.ts', symbols: [] },
    {
      path: 'main.ts',
      symbols: [
        {
          id: 'main.ts#bootstrap',
          qualified_name: 'bootstrap',
          kind: 'function',
          line_start: 1,
          line_end: 4,
          parent: null,
          notes: 0,
        },
      ],
    },
  ],
}
const FILES = { 'GET /api/repos': { body: REPOS }, 'GET /api/repos/1/tree': { body: TREE } }
const SERVICE_VIEW: SymbolView = {
  ...view,
  symbol: { ...view.symbol, id: SERVICE, qualified_name: 'AppService.find' },
  entrypoints: [],
  summary: { flow: null, symbol: 'Finds a user by id.' },
  steps: [],
  callers: [
    {
      id: CONTROLLER,
      qualified_name: 'AppController.find',
      kind: 'method',
      file: 'src/app.controller.ts',
      line_start: 8,
      line_end: 11,
      notes: 1,
    },
  ],
  notes: [],
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
const REVIEW_URL = `${SYMBOL_URL}/review`
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
  beforeEach(() => localStorage.clear())

  it('lists the features of the first repository with their note counts, and filters on the server', async () => {
    mockFetch({
      ...BASE,
      'GET /api/repos/1/features?q=u': { body: { features: [feature()], total: 1 } },
    })

    renderAt('/code')

    expect(await screen.findByText('GET /users/:id')).toBeInTheDocument()
    expect(screen.getByText('bootstrap')).toBeInTheDocument()
    expect(screen.getByText('2 features · 0 reviewed')).toBeInTheDocument()
    expect(screen.getByText('Pick a feature to see what it does.')).toBeInTheDocument()

    // One character: every keystroke is a request, and every URL must be mocked.
    await userEvent.type(screen.getByLabelText('Filter features'), 'u')

    expect(await screen.findByText('1 feature · 0 reviewed')).toBeInTheDocument()
    expect(screen.queryByText('bootstrap')).toBeNull()
  })

  it('scrolls the feature list, not the page, to the open feature', async () => {
    mockFetch({ ...BASE, [`GET ${SYMBOL_URL}`]: { body: view } })
    // jsdom has no layout: the open feature sits 900px down a list whose box ends at 300px.
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(function (this: HTMLElement) {
        const top = this.getAttribute('aria-current') === 'true' ? 900 : 0
        return { top, bottom: top + (top ? 20 : 300), height: top ? 20 : 300 } as DOMRect
      })
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => {})

    renderAt(AT_SYMBOL)
    const open = (await screen.findByText('GET /users/:id')).closest('button') as HTMLElement
    expect(open).toHaveAttribute('aria-current', 'true')

    const list = open.closest('.overflow-y-auto') as HTMLElement
    await waitFor(() => expect(list.scrollTop).toBeGreaterThan(600))
    expect(scrollTo).not.toHaveBeenCalled()
    rect.mockRestore()
    scrollTo.mockRestore()
  })

  it('groups features by kind under headings when there is more than one kind', async () => {
    mockFetch({
      ...BASE,
      'GET /api/repos/1/features': {
        body: {
          features: [
            feature({ kind: 'cron', route: '0 * * * *', symbol_id: 'src/a.ts#A.tick' }),
            feature(),
            feature({ kind: 'page', route: '/dashboard', symbol_id: 'app/dashboard/page.tsx#D' }),
          ],
          total: 3,
        },
      },
    })

    renderAt('/code')

    expect(await screen.findByText('/dashboard')).toBeInTheDocument()
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(headings).toEqual(['Routes', 'Pages', 'Cron'])
  })

  it('says when the features are top-level functions found without a framework adapter', async () => {
    mockFetch({
      ...BASE,
      'GET /api/repos/1/features': {
        body: {
          features: [feature({ kind: 'root', route: 'main', framework: 'generic' })],
          total: 1,
        },
      },
    })

    renderAt('/code')

    expect(
      await screen.findByText(
        '1 feature · 0 reviewed · no framework adapter: showing top-level functions',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull()
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

    // A business rule needs no developer review: saved without `proposed`.
    await userEvent.click(screen.getByRole('button', { name: 'Add context' }))
    const form = screen.getByRole('form', { name: 'Add context' })
    expect(within(form).getByText('AppController.find')).toBeInTheDocument() // the target
    expect(within(form).queryByRole('button', { name: 'Change' })).toBeNull()
    expect(screen.getByRole('radio', { name: /Business rule/ })).toBeChecked()
    await userEvent.type(
      screen.getByLabelText('Which rule must this feature follow?'),
      'Only admins.',
    )
    expect(screen.getByText(/Saved as verified: answers cite it at once\./)).toBeInTheDocument()
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
    expect(screen.queryByRole('form', { name: 'Add context' })).toBeNull() // closes after a save
  })

  it('sends a mismatch as a proposed expected behaviour, signed with the name', async () => {
    const fetch = mockFetch({
      ...BASE,
      [`GET ${SYMBOL_URL}`]: { body: view },
      'POST /api/annotations': { status: 201, body: note(4, { status: 'proposed' }) },
    })

    renderAt(AT_SYMBOL)
    await userEvent.type(await screen.findByLabelText('Your name'), 'Alice')
    await userEvent.click(await screen.findByRole('button', { name: 'Not right' }))
    await userEvent.type(
      screen.getByLabelText('What should happen instead?'),
      'Admins only see their own users.',
    )
    expect(screen.getByText(/Saved as a proposal: a maintainer reviews it/)).toBeInTheDocument()
    expect(screen.getByText(/Signed as Alice\./)).toBeInTheDocument()
    expect(screen.getByLabelText('How answers cite it')).toHaveTextContent('note#… [proposed]')
    expect(screen.getByText('Answers do not cite it until a maintainer verifies it.')).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(bodyOf(fetch, 'POST /api/annotations')).toBeDefined())
    expect(bodyOf(fetch, 'POST /api/annotations')).toEqual({
      target_kind: 'symbol',
      target_ref: CONTROLLER,
      repo: 1,
      scope: 'deps',
      text: 'Admins only see their own users.',
      note_type: 'expected_behavior',
      by: 'Alice',
      proposed: true,
    })
    expect(localStorage.getItem('enigram.by')).toBe('Alice')
  })

  it('proposes a known issue written as context with ⌘+Enter, and cancels without saving', async () => {
    const fetch = mockFetch({
      ...BASE,
      [`GET ${SYMBOL_URL}`]: { body: view },
      'POST /api/annotations': { status: 201, body: note(4) },
    })

    renderAt(AT_SYMBOL)
    await userEvent.click(await screen.findByRole('button', { name: 'Add context' }))
    await userEvent.click(screen.getByRole('radio', { name: /Known issue/ }))
    await userEvent.type(
      screen.getByLabelText('What goes wrong, and when?'),
      'Times out.{Meta>}{Enter}{/Meta}',
    )

    await waitFor(() => expect(bodyOf(fetch, 'POST /api/annotations')).toBeDefined())
    expect(bodyOf(fetch, 'POST /api/annotations')).toMatchObject({
      note_type: 'known_issue',
      proposed: true,
    })

    // Switching mode keeps the text and moves to that mode's type.
    await userEvent.click(screen.getByRole('button', { name: 'Add context' }))
    await userEvent.type(screen.getByLabelText('Which rule must this feature follow?'), 'Draft')
    await userEvent.click(screen.getByRole('button', { name: 'Not right' }))
    expect(screen.getByLabelText('What should happen instead?')).toHaveValue('Draft')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('form', { name: 'Not right' })).toBeNull()
  })

  it('marks the summary as right with the name kept in the browser', async () => {
    localStorage.setItem('enigram.by', 'Alice')
    const fetch = mockFetch({
      ...BASE,
      [`GET ${SYMBOL_URL}`]: { body: view },
      [`POST ${REVIEW_URL}`]: {
        body: { by: 'Alice', actor: 'web', at: '2026-11-07T08:00:00Z', current: true },
      },
    })

    renderAt(AT_SYMBOL)
    expect(await screen.findByLabelText('Your name')).toHaveValue('Alice')
    await userEvent.click(await screen.findByRole('button', { name: 'Looks right' }))

    await waitFor(() => expect(bodyOf(fetch, `POST ${REVIEW_URL}`)).toEqual({ by: 'Alice' }))
    // Reloaded afterwards: the marker and the left list come back from the API.
    await waitFor(() =>
      expect(fetch.mock.calls.filter(([url]) => url === SYMBOL_URL)).toHaveLength(2),
    )
  })

  it('shows who reviewed the summary, lets them undo it, and says when it went stale', async () => {
    const at = '2026-11-07T08:00:00Z'
    const reviewed = { ...view, review: { by: 'Alice', actor: 'web', at, current: true } }
    const fetch = mockFetch({
      ...BASE,
      [`GET ${SYMBOL_URL}`]: { body: reviewed },
      [`DELETE ${REVIEW_URL}`]: { body: null },
    })

    renderAt(AT_SYMBOL)

    expect(await screen.findByText(/Reviewed by Alice/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Looks right' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() =>
      expect(fetch.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(true),
    )
  })

  it.each([
    ['viewer', 'me', [], false],
    ['contributor', 'web', ['Looks right', 'Not right', 'Add context'], true],
    ['contributor', 'me', ['Looks right', 'Not right', 'Add context'], false],
    ['maintainer', 'me', ['Looks right', 'Not right', 'Add context'], true],
  ] as const)(
    'shows a %s (key %s) the review actions its role allows',
    async (role, name, actions, undo) => {
      const stale = {
        ...view,
        review: { by: '', actor: 'web', at: '2026-11-01T08:00:00Z', current: false },
      }
      mockFetch({ ...BASE, [`GET ${SYMBOL_URL}`]: { body: stale } })
      render(
        asRole(
          role,
          <MemoryRouter initialEntries={[AT_SYMBOL]}>
            <Routes>
              <Route path="/code" element={<Code />} />
            </Routes>
          </MemoryRouter>,
          name,
        ),
      )

      expect(await screen.findByText(/Reviewed by web/)).toBeInTheDocument()
      for (const label of ['Looks right', 'Not right', 'Add context'])
        expect(!!screen.queryByRole('button', { name: label })).toBe(
          (actions as readonly string[]).includes(label),
        )
      expect(!!screen.queryByRole('button', { name: 'Remove' })).toBe(undo)
    },
  )

  it('offers a new review when the summary changed since the last one', async () => {
    const stale = {
      ...view,
      review: { by: '', actor: 'web', at: '2026-11-01T08:00:00Z', current: false },
    }
    mockFetch({ ...BASE, [`GET ${SYMBOL_URL}`]: { body: stale } })

    renderAt(AT_SYMBOL)

    expect(
      await screen.findByText(/Reviewed by web .* the summary changed since/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Looks right' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove' })).toBeInTheDocument()
  })

  it('shows which features are reviewed and how many', async () => {
    mockFetch({
      ...BASE,
      'GET /api/repos/1/features': {
        body: { ...FEATURES, features: [feature({ reviewed: true }), FEATURES.features[1]] },
      },
    })

    renderAt('/code')

    expect(await screen.findByText('2 features · 1 reviewed')).toBeInTheDocument()
    expect(screen.getAllByLabelText('reviewed')).toHaveLength(1)
  })

  describe('re-check of a flagged note', () => {
    const PATCH = 'PATCH /api/annotations/3'
    const flagged = (changes: Partial<Annotation> = {}): SymbolView => ({
      ...view,
      notes: [
        note(3, {
          status: 'needs_verification',
          reason: 'hash',
          summary_kind: 'flow',
          summary_text: '1. `AppService.find` reads a cache.',
          ...changes,
        }),
      ],
    })

    it('shows the summary when written next to the summary now', async () => {
      mockFetch({ ...BASE, [`GET ${SYMBOL_URL}`]: { body: flagged() } })

      renderAt(AT_SYMBOL)

      expect(
        await screen.findByText('Re-check: The code of this symbol changed.'),
      ).toBeInTheDocument()
      expect(screen.getByText('Summary when written (flow)')).toBeInTheDocument()
      expect(screen.getByText('reads a cache.', { exact: false })).toBeInTheDocument()
      expect(screen.getByText('Summary now (flow)')).toBeInTheDocument()
      // The main summary and the "now" of the re-check: the same text twice.
      expect(screen.getAllByText('loads the user.', { exact: false })).toHaveLength(2)
    })

    it.each([
      ['Still right', { status: 'verified', conflict: false }],
      ['Code is wrong now', { status: 'verified', conflict: true }],
      ['Note is outdated', { status: 'rejected' }],
    ])('%s sends its change and reloads', async (name, body) => {
      const fetch = mockFetch({
        ...BASE,
        [`GET ${SYMBOL_URL}`]: { body: flagged() },
        [PATCH]: { body: note(3) },
      })

      renderAt(AT_SYMBOL)
      await userEvent.click(await screen.findByRole('button', { name }))

      await waitFor(() => expect(bodyOf(fetch, PATCH)).toEqual(body))
      await waitFor(() =>
        expect(fetch.mock.calls.filter(([url]) => url === SYMBOL_URL)).toHaveLength(2),
      )
    })

    it('says when the summary did not change, or the note kept none', async () => {
      mockFetch({
        ...BASE,
        [`GET ${SYMBOL_URL}`]: {
          body: {
            ...flagged({ summary_text: view.summary.flow }),
            notes: [
              ...flagged({ summary_text: view.summary.flow }).notes,
              note(5, { status: 'needs_verification', reason: 'deps' }),
            ],
          },
        },
      })

      renderAt(AT_SYMBOL)

      expect(await screen.findByText('Unchanged since the note was written.')).toBeInTheDocument()
      expect(screen.getByText('Re-check: Code it calls changed.')).toBeInTheDocument()
      expect(screen.getByText('No summary was saved with this note.')).toBeInTheDocument()
    })

    it('compares with the summary shown now, not the kind the note kept', async () => {
      // Written against a flow; the symbol is no longer an entry point and has its own summary.
      const moved = { ...flagged(), summary: { flow: null, symbol: 'Finds one user.' } }
      mockFetch({ ...BASE, [`GET ${SYMBOL_URL}`]: { body: moved } })

      renderAt(AT_SYMBOL)

      expect(await screen.findByText('Summary now (symbol)')).toBeInTheDocument()
      expect(screen.queryByText(/No summary yet/)).toBeNull()
      // The main summary and the "now" of the re-check.
      expect(screen.getAllByText('Finds one user.')).toHaveLength(2)
    })

    it('says the reason for an orphan flag without claiming the note moved', async () => {
      mockFetch({ ...BASE, [`GET ${SYMBOL_URL}`]: { body: flagged({ reason: 'orphan' }) } })

      renderAt(AT_SYMBOL)

      expect(
        await screen.findByText(
          'Re-check: The note was moved, or its symbol was removed and came back.',
        ),
      ).toBeInTheDocument()
    })

    it('shows the API error when the key may not verify the note', async () => {
      mockFetch({
        ...BASE,
        [`GET ${SYMBOL_URL}`]: { body: flagged() },
        [PATCH]: { status: 403, body: { detail: 'Only the owner (dev) may verify this note' } },
      })

      renderAt(AT_SYMBOL)
      await userEvent.click(await screen.findByRole('button', { name: 'Still right' }))

      expect(await screen.findByText(/Only the owner \(dev\)/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Still right' })).toBeEnabled()
    })

    it('shows no re-check on a verified note, and marks a conflict', async () => {
      mockFetch({
        ...BASE,
        [`GET ${SYMBOL_URL}`]: { body: { ...view, notes: [note(3, { conflict: true })] } },
      })

      renderAt(AT_SYMBOL)

      expect(await screen.findByText('Conflicts with code')).toBeInTheDocument()
      expect(screen.queryByText('Summary now')).toBeNull()
      expect(screen.queryByRole('button', { name: 'Still right' })).toBeNull()
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

  describe('Files mode', () => {
    it('lists files by folder and opens a file to its symbol outline', async () => {
      const fetch = mockFetch(FILES)

      renderAt('/code?repo=1&view=files')

      expect(await screen.findByText('4 files · 4 symbols')).toBeInTheDocument()
      const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
      expect(headings).toEqual(['(root)', 'src'])
      expect(screen.getByText('Pick a symbol to see what it does.')).toBeInTheDocument()
      // Closed until opened; a file without symbols cannot be opened.
      expect(screen.queryByRole('button', { name: /AppService\.find/ })).toBeNull()
      expect(screen.getByRole('button', { name: /types\.ts/ })).toBeDisabled()

      await userEvent.click(screen.getByRole('button', { name: /app\.controller\.ts/ }))

      expect(screen.getByRole('button', { name: /^class AppController/ })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /^method find/ })).toBeInTheDocument()
      expect(fetch.mock.calls.map(([url]) => url)).not.toContain('/api/repos/1/features')
    })

    it('opens a symbol from the outline in the same panels as a feature', async () => {
      mockFetch({ ...FILES, [`GET ${SYMBOL_URL}`]: { body: view } })

      renderAt('/code?repo=1&view=files')
      await userEvent.click(await screen.findByRole('button', { name: /app\.controller\.ts/ }))
      await userEvent.click(screen.getByRole('button', { name: /^method find/ }))

      expect(await screen.findByRole('heading', { name: 'AppController.find' })).toBeInTheDocument()
      expect(screen.getByText('Notes (1)')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /^method find/ })).toHaveAttribute(
        'aria-current',
        'true',
      )
    })

    it('opens the file of a linked symbol', async () => {
      mockFetch({ ...FILES, [`GET ${SERVICE_URL}`]: { body: SERVICE_VIEW } })

      renderAt(`/code?repo=1&view=files&symbol=${encodeURIComponent(SERVICE)}`)

      expect(await screen.findByRole('button', { name: /^method find/ })).toHaveAttribute(
        'aria-current',
        'true',
      )
      expect(screen.queryByRole('button', { name: /^class AppController/ })).toBeNull()
    })

    it('filters files by path or symbol name, opening the files a symbol matched in', async () => {
      mockFetch(FILES)

      renderAt('/code?repo=1&view=files')
      await screen.findByText('4 files · 4 symbols')
      await userEvent.type(screen.getByLabelText('Filter files'), 'boot')

      expect(screen.getByText('1 of 4 files')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /^function bootstrap/ })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /app\.service\.ts/ })).toBeNull()

      await userEvent.clear(screen.getByLabelText('Filter files'))
      await userEvent.type(screen.getByLabelText('Filter files'), 'nothing')
      expect(screen.getByText('No files match.')).toBeInTheDocument()
    })

    it('scrolls the list, not the page, to the open symbol', async () => {
      mockFetch({ ...FILES, [`GET ${SERVICE_URL}`]: { body: SERVICE_VIEW } })
      // jsdom has no layout: the open symbol sits 900px down a list whose box ends at 300px.
      const rect = vi
        .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
        .mockImplementation(function (this: HTMLElement) {
          const top = this.getAttribute('aria-current') === 'true' ? 900 : 0
          return { top, bottom: top + (top ? 20 : 300), height: top ? 20 : 300 } as DOMRect
        })
      const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => {})

      renderAt(`/code?repo=1&view=files&symbol=${encodeURIComponent(SERVICE)}`)
      const open = await screen.findByRole('button', { name: /^method find/ })

      const list = open.closest('.overflow-y-auto') as HTMLElement
      await waitFor(() => expect(list.scrollTop).toBeGreaterThan(600))
      expect(scrollTo).not.toHaveBeenCalled()
      rect.mockRestore()
      scrollTo.mockRestore()
    })

    it('keeps the file of the open symbol in a filtered list', async () => {
      mockFetch({ ...FILES, [`GET ${SERVICE_URL}`]: { body: SERVICE_VIEW } })

      // A caller opened from another file stays visible although the filter misses it.
      renderAt(`/code?repo=1&view=files&q=boot&symbol=${encodeURIComponent(SERVICE)}`)

      expect(await screen.findByRole('button', { name: /^method find/ })).toHaveAttribute(
        'aria-current',
        'true',
      )
      expect(screen.getByRole('button', { name: /^function bootstrap/ })).toBeInTheDocument()
      expect(screen.getByText('2 of 4 files')).toBeInTheDocument()
    })

    it('switches between features and files, keeping the open symbol', async () => {
      mockFetch({ ...BASE, ...FILES, [`GET ${SYMBOL_URL}`]: { body: view } })

      renderAt(AT_SYMBOL)
      expect(await screen.findByText('GET /users/:id')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Features' })).toHaveAttribute(
        'aria-pressed',
        'true',
      )

      await userEvent.click(screen.getByRole('button', { name: 'Files' }))

      expect(await screen.findByText('4 files · 4 symbols')).toBeInTheDocument()
      expect(screen.queryByText('GET /users/:id')).toBeNull()
      expect(screen.getByRole('heading', { name: 'AppController.find' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /^method find/ })).toHaveAttribute(
        'aria-current',
        'true',
      )

      await userEvent.click(screen.getByRole('button', { name: 'Features' }))
      expect(await screen.findByText('GET /users/:id')).toBeInTheDocument()
    })
  })

  it('lists the callers of a symbol and opens one', async () => {
    mockFetch({
      ...BASE,
      [`GET ${SERVICE_URL}`]: { body: SERVICE_VIEW },
      [`GET ${SYMBOL_URL}`]: { body: view },
    })

    renderAt(`/code?repo=1&symbol=${encodeURIComponent(SERVICE)}`)

    expect(await screen.findByText('Called by (1)')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'AppController.find' }))

    expect(await screen.findByRole('heading', { name: 'AppController.find' })).toBeInTheDocument()
    expect(screen.getByText('Called by (0)')).toBeInTheDocument()
    expect(screen.getByText(/Nothing in the repository calls it/)).toBeInTheDocument()
  })

  it('asks for a repository when none is indexed', async () => {
    const fetch = mockFetch({ 'GET /api/repos': { body: [] } })

    renderAt('/code')

    expect(await screen.findByText(/No repository indexed yet/)).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})

import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DocumentRow, Job, RepoStat } from '../api'
import { asRole } from '../test/access'
import { mockFetch } from '../test/mockFetch'
import Pipeline from './Pipeline'

function row(book: string, changes: Partial<DocumentRow> = {}): DocumentRow {
  return {
    book,
    file: `${book}.pdf`,
    title: null,
    status: 'indexed',
    pages: 5,
    chunks: 13,
    error: null,
    updated_at: '2026-10-26T07:02:00Z',
    repo: null,
    job: null,
    ...changes,
  }
}

const URL = 'https://github.com/org/handbook'

const REPO: RepoStat = {
  id: 4,
  name: 'org-handbook',
  url: URL,
  ref: null,
  indexed_sha: 'abc1234def',
  commit_at: '2026-11-01T10:00:00Z',
  indexed_at: '2026-11-01T10:05:00Z',
  remote: { status: 'current', sha: null, checked_at: '2026-11-01T11:00:00Z', error: null },
  updates: 'poll',
  last_run: null,
  files: 12,
  symbols: 0,
  entrypoints: 0,
  documents: 12,
}

function job(id: number, changes: Partial<Job> = {}): Job {
  return {
    id,
    kind: 'index',
    payload: { book: 'a', file: 'a.pdf' },
    status: 'queued',
    result: null,
    error: null,
    created_at: '2026-10-26T07:00:00Z',
    started_at: null,
    finished_at: null,
    ...changes,
  }
}

const repoJob = (changes: Partial<Job> = {}) =>
  job(41, { kind: 'index_repo', payload: { url: URL }, ...changes })

// The routes every load reads, with what a test gives on top.
function routes(extra: Parameters<typeof mockFetch>[0] = {}) {
  return mockFetch({
    'GET /api/documents': { body: [] },
    'GET /api/jobs': { body: [] },
    'GET /api/stats/repos': { body: [] },
    ...extra,
  })
}

const page = (ui = <Pipeline />) => render(<MemoryRouter>{ui}</MemoryRouter>)

async function openAdd(user = userEvent.setup()) {
  await user.click(await screen.findByRole('button', { name: 'Add source' }))
  return screen.getByRole('dialog', { name: 'Add source' })
}

describe('Pipeline', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('keeps polling while the API keeps failing with the same error', async () => {
    vi.useFakeTimers()
    let calls = 0
    const fetch = vi.fn(async (url: string) =>
      url === '/api/documents'
        ? ++calls === 1
          ? Response.json([row('a', { status: 'queued' })])
          : Response.json({ detail: 'API restarting' }, { status: 502 })
        : Response.json([]),
    )
    vi.stubGlobal('fetch', fetch)
    page()

    await act(() => vi.advanceTimersByTimeAsync(0))
    for (let i = 0; i < 3; i++) await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(fetch.mock.calls.filter(([url]) => url === '/api/documents')).toHaveLength(4)
  })

  it('lists repositories and files in one list, with status and errors', async () => {
    routes({
      'GET /api/documents': {
        body: [
          row('paper', { title: 'On Autopilot?' }),
          row('broken', { status: 'failed', error: 'parse: bad PDF', pages: null }),
        ],
      },
      'GET /api/stats/repos': { body: [REPO] },
    })
    page()

    const list = await screen.findByRole('list', { name: 'Sources' })
    const names = within(list)
      .getAllByRole('button')
      .map((b) => b.getAttribute('aria-label'))
    // what failed first, then by name
    expect(names).toEqual(['Open broken', 'Open On Autopilot?', 'Open org-handbook'])
    expect(within(list).getByText('indexed')).toHaveClass('status-indexed')
    expect(within(list).getByText('failed')).toHaveClass('status-failed')
    expect(within(list).getByText('parse: bad PDF')).toBeInTheDocument()
    expect(within(list).getByText('Up to date')).toHaveClass('status-current')
  })

  it('shows an empty state with the next step', async () => {
    routes()
    page()

    expect(await screen.findByText(/No sources yet/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Add source' })).toHaveLength(2)
  })

  it('groups documents from a repository under it', async () => {
    routes({
      'GET /api/documents': {
        body: [row('org-handbook--README', { repo: URL, title: 'Readme' }), row('paper')],
      },
    })
    const user = userEvent.setup()
    page()

    const list = await screen.findByRole('list', { name: 'Sources' })
    expect(within(list).getAllByRole('button')).toHaveLength(2) // the repository and paper
    expect(within(list).getByText('github.com/org/handbook · 1 docs')).toBeInTheDocument()

    await user.click(within(list).getByRole('button', { name: 'Open github.com/org/handbook' }))
    const docs = screen.getByRole('region', { name: 'Documents from this repository' })
    expect(within(docs).getByText('Readme')).toBeInTheDocument()
  })

  it('filters by kind and by name', async () => {
    routes({
      'GET /api/documents': { body: [row('paper'), row('notes')] },
      'GET /api/stats/repos': { body: [REPO] },
    })
    const user = userEvent.setup()
    page()
    const list = await screen.findByRole('list', { name: 'Sources' })

    await user.click(screen.getByRole('button', { name: 'Repositories 1' }))
    expect(within(list).getAllByRole('button')).toHaveLength(1)

    await user.click(screen.getByRole('button', { name: 'All 3' }))
    await user.type(screen.getByLabelText('Filter sources'), 'pap')
    expect(
      within(list)
        .getAllByRole('button')
        .map((b) => b.textContent),
    ).toEqual([expect.stringContaining('paper')])

    await user.type(screen.getByLabelText('Filter sources'), 'zzz')
    expect(screen.getByText('No source matches.')).toBeInTheDocument()
  })

  it('uploads files from the drawer and shows each job', async () => {
    const fetch = routes({
      'GET /api/documents': { body: [row('a', { status: 'queued' })] },
      'POST /api/documents': { status: 202, body: { book: 'a', status: 'queued', job: job(7) } },
    })
    const user = userEvent.setup()
    page()
    const drawer = await openAdd(user)

    await user.click(within(drawer).getByRole('tab', { name: 'Upload files' }))
    await user.upload(
      within(drawer).getByLabelText(/Drop PDF or Markdown/),
      new File(['%PDF-1.4'], 'a.pdf', { type: 'application/pdf' }),
    )

    expect(await within(drawer).findByText('queued (job 7)')).toBeInTheDocument()
    const post = fetch.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect((post[1]!.body as FormData).get('file')).toBeInstanceOf(File)
  })

  it('shows the API message when an upload is rejected', async () => {
    routes({
      'POST /api/documents': {
        status: 409,
        body: { detail: 'a.pdf differs from the indexed version' },
      },
    })
    const user = userEvent.setup()
    page()
    const drawer = await openAdd(user)
    await user.click(within(drawer).getByRole('tab', { name: 'Upload files' }))

    await user.upload(
      within(drawer).getByLabelText(/Drop PDF or Markdown/),
      new File(['x'], 'a.pdf', { type: 'application/pdf' }),
    )

    expect(await within(drawer).findByText('a.pdf differs from the indexed version')).toHaveClass(
      'error-text',
    )
  })

  it('asks before deleting, warns about rebuilt entities and can be cancelled', async () => {
    const fetch = routes({ 'GET /api/documents': { body: [row('paper')] } })
    const user = userEvent.setup()
    page()

    await user.click(await screen.findByRole('button', { name: 'Open paper' }))
    await user.click(screen.getByRole('button', { name: 'Delete paper' }))
    expect(screen.getByText(/drops manual graph edits/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'No' }))

    expect(fetch.mock.calls.every(([, init]) => init?.method !== 'DELETE')).toBe(true)
    expect(screen.getByRole('button', { name: 'Delete paper' })).toBeInTheDocument()
  })

  it('deletes a document and polls until it is gone', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    let listed: DocumentRow[] = [row('paper')]
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') {
        listed = [row('paper', { status: 'deleting' })]
        return Response.json({ book: 'paper', status: 'deleting', job: null }, { status: 202 })
      }
      return Response.json(url === '/api/documents' ? listed : [])
    })
    vi.stubGlobal('fetch', fetch)
    page()

    await user.click(await screen.findByRole('button', { name: 'Open paper' }))
    await user.click(screen.getByRole('button', { name: 'Delete paper' }))
    await user.click(screen.getByRole('button', { name: 'Yes' }))

    expect(fetch).toHaveBeenCalledWith(
      '/api/documents/paper',
      expect.objectContaining({ method: 'DELETE' }),
    )
    const drawer = await screen.findByRole('dialog')
    expect(await within(drawer).findAllByText('deleting')).not.toHaveLength(0)
    expect(screen.queryByRole('button', { name: 'Delete paper' })).not.toBeInTheDocument()

    listed = []
    await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(screen.getByText(/No sources yet/)).toBeInTheDocument()
  })

  it('shows why a delete was refused', async () => {
    routes({
      'GET /api/documents': { body: [row('paper')] },
      'DELETE /api/documents/paper': {
        status: 409,
        body: { detail: 'paper has a queued or running job; wait for it to finish' },
      },
    })
    const user = userEvent.setup()
    page()

    await user.click(await screen.findByRole('button', { name: 'Open paper' }))
    await user.click(screen.getByRole('button', { name: 'Delete paper' }))
    await user.click(screen.getByRole('button', { name: 'Yes' }))

    expect(await screen.findByText(/wait for it to finish/)).toHaveClass('error-box')
  })

  it('syncs a repository and shows each stage, then the summary', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    let active: Job[] = []
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        active = [repoJob()]
        return Response.json({ repo: URL, job: repoJob() }, { status: 202 })
      }
      if (url === '/api/jobs') return Response.json(active)
      if (url === '/api/jobs/41')
        return Response.json(
          repoJob({
            status: 'done',
            result: { files: 3, indexed: ['a', 'b'], unchanged: 1, deleted: [] },
          }),
        )
      return Response.json([])
    })
    vi.stubGlobal('fetch', fetch)
    page()
    const drawer = await openAdd(user)

    await user.type(within(drawer).getByLabelText('Repository URL'), URL)
    await user.type(within(drawer).getByLabelText('Branch or tag'), 'v2')
    await user.click(within(drawer).getByRole('button', { name: 'Start sync' }))

    const call = fetch.mock.calls.find(([url]) => url === '/api/repos')
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ url: URL, ref: 'v2' })
    expect(screen.queryByRole('dialog', { name: 'Add source' })).not.toBeInTheDocument()
    const list = await screen.findByRole('list', { name: 'Sources' })
    const stages = () => within(list).getByRole('list', { name: 'Sync stages' })
    expect(within(stages()).getByRole('listitem', { current: 'step' })).toHaveTextContent('Queued')
    expect(within(list).getByText('Waiting for the worker')).toBeInTheDocument()

    active = [
      repoJob({
        status: 'running',
        started_at: '2026-10-26T07:00:05Z',
        progress: {
          stage: 'docs',
          detail: { stages: ['clone', 'scan', 'code', 'docs'], markdown: 3, code: 'typescript' },
          at: '2026-10-26T07:00:30Z',
        },
      }),
    ]
    await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(within(stages()).getByRole('listitem', { current: 'step' })).toHaveTextContent(
      'Documents',
    )
    expect(
      within(stages())
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual([
      'Queued (done)',
      'Clone (done)',
      'Scan (done)',
      'Code graph (done)',
      'Documents (in progress)',
    ])
    expect(within(list).getByText('· found TypeScript code · 3 Markdown files')).toBeInTheDocument()
    expect(within(list).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '4')

    active = []
    await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(within(list).queryByRole('list', { name: 'Sync stages' })).toBeNull()
    expect(
      within(list).getByText('Synced: 3 files, 2 indexed, 1 unchanged, 0 deleted'),
    ).toBeInTheDocument()
  })

  it('picks up a sync already running when the page opens', async () => {
    routes({
      'GET /api/stats/repos': { body: [REPO] },
      'GET /api/jobs': {
        body: [
          repoJob({
            status: 'running',
            started_at: '2026-10-26T07:00:05Z',
            progress: { stage: 'clone', detail: { stages: ['clone', 'scan'] }, at: '' },
          }),
        ],
      },
    })
    page()

    const list = await screen.findByRole('list', { name: 'Sources' })
    const stages = await within(list).findByRole('list', { name: 'Sync stages' })
    // Before the scan only clone and scan are known; "Index" stands for the rest.
    expect(
      within(stages)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['Queued (done)', 'Clone (in progress)', 'Scan', 'Index'])
    expect(within(list).getByText('Syncing')).toHaveClass('status-running')
    expect(screen.getByRole('status')).toHaveTextContent('1 in progress')
  })

  it('shows why a repository was refused', async () => {
    routes({
      'POST /api/repos': {
        status: 422,
        body: { detail: "Host '10.0.0.5' is not in REPO_HOSTS (github.com)" },
      },
    })
    const user = userEvent.setup()
    page()
    const drawer = await openAdd(user)

    await user.type(within(drawer).getByLabelText('Repository URL'), 'https://10.0.0.5/org/x')
    await user.click(within(drawer).getByRole('button', { name: 'Start sync' }))

    expect(await within(drawer).findByText(/not in REPO_HOSTS/)).toHaveClass('error-box')
  })

  it('sends the access token of a private repository', async () => {
    const fetch = routes({
      'POST /api/repos': {
        status: 202,
        body: { repo: 'https://github.com/org/private', job: repoJob() },
      },
    })
    const user = userEvent.setup()
    page()
    const drawer = await openAdd(user)

    await user.type(
      within(drawer).getByLabelText('Repository URL'),
      'https://github.com/org/private',
    )
    const token = within(drawer).getByLabelText('Access token')
    expect(token).toHaveAttribute('type', 'password')
    await user.type(token, 'github_pat_11ABC')
    await user.click(within(drawer).getByRole('button', { name: 'Start sync' }))

    const call = fetch.mock.calls.find(([url]) => url === '/api/repos')
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      url: 'https://github.com/org/private',
      token: 'github_pat_11ABC',
    })
  })

  it('opens a repository with its state, re-index and a link to its full page', async () => {
    const fetch = routes({
      'GET /api/stats/repos': { body: [REPO] },
      'POST /api/repos': { status: 202, body: { repo: URL, job: repoJob() } },
    })
    const user = userEvent.setup()
    page()

    await user.click(await screen.findByRole('button', { name: 'Open org-handbook' }))
    const drawer = screen.getByRole('dialog')
    expect(within(drawer).getByRole('link', { name: 'Open full details' })).toHaveAttribute(
      'href',
      '/pipeline/repos/4',
    )
    expect(within(drawer).getByText('abc1234')).toBeInTheDocument()

    await user.click(within(drawer).getByRole('button', { name: 'Re-index now' }))

    const call = fetch.mock.calls.find(([url]) => url === '/api/repos')
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ url: URL })
    expect(await within(drawer).findByText('Queued', { selector: 'h3' })).toBeInTheDocument()
    expect(within(drawer).getByRole('button', { name: 'Re-index now' })).toBeDisabled()
  })
})

describe('Pipeline by role', () => {
  it('shows a viewer the list without add or delete', async () => {
    routes({ 'GET /api/documents': { body: [row('paper')] } })
    const user = userEvent.setup()
    page(asRole('viewer', <Pipeline />))

    await user.click(await screen.findByRole('button', { name: 'Open paper' }))
    expect(screen.queryByRole('button', { name: 'Add source' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete paper' })).toBeNull()
    expect(screen.getByText(/need the maintainer role \(yours: viewer\)/)).toBeInTheDocument()
  })

  it('asks a maintainer for no repository token', async () => {
    routes({ 'GET /api/documents': { body: [row('paper')] } })
    page(asRole('maintainer', <Pipeline />))
    const drawer = await openAdd()

    expect(within(drawer).getByLabelText('Branch or tag')).toBeInTheDocument()
    expect(within(drawer).queryByLabelText('Access token')).toBeNull()
  })
})

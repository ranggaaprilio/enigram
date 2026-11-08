import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DocumentRow } from '../api'
import { mockFetch } from '../test/mockFetch'
import Documents from './Documents'

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

const job = (id: number) => ({
  id,
  kind: 'index',
  payload: { book: 'a', file: 'a.pdf' },
  status: 'queued',
  result: null,
  error: null,
  created_at: '2026-10-26T07:00:00Z',
  started_at: null,
  finished_at: null,
})

describe('Documents', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('keeps polling while the API keeps failing with the same error', async () => {
    vi.useFakeTimers()
    let calls = 0
    const fetch = vi.fn(async () =>
      ++calls === 1
        ? Response.json([row('a', { status: 'queued' })])
        : Response.json({ detail: 'API restarting' }, { status: 502 }),
    )
    vi.stubGlobal('fetch', fetch)
    render(<Documents />)

    await act(() => vi.advanceTimersByTimeAsync(0))
    for (let i = 0; i < 3; i++) await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(fetch).toHaveBeenCalledTimes(4)
  })

  it('lists documents with their status and errors', async () => {
    mockFetch({
      'GET /api/documents': {
        body: [
          row('paper', { title: 'On Autopilot?' }),
          row('broken', { status: 'failed', error: 'parse: bad PDF', pages: null }),
        ],
      },
    })

    render(<Documents />)

    expect(await screen.findByText('On Autopilot?')).toBeInTheDocument()
    expect(screen.getByText('indexed')).toHaveClass('status-indexed')
    expect(screen.getByText('failed')).toHaveClass('status-failed')
    expect(screen.getByText('parse: bad PDF')).toBeInTheDocument()
  })

  it('shows an empty state', async () => {
    mockFetch({ 'GET /api/documents': { body: [] } })

    render(<Documents />)

    expect(await screen.findByText(/No documents yet/)).toBeInTheDocument()
  })

  it('uploads a file and shows its job', async () => {
    const fetch = mockFetch({
      'GET /api/documents': { body: [row('a', { status: 'queued', job: null })] },
      'POST /api/documents': { status: 202, body: { book: 'a', status: 'queued', job: job(7) } },
    })
    render(<Documents />)
    const file = new File(['%PDF-1.4'], 'a.pdf', { type: 'application/pdf' })

    await userEvent.upload(screen.getByLabelText(/Drop PDF or Markdown/), file)

    expect(await screen.findByText('queued (job 7)')).toBeInTheDocument()
    const post = fetch.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect((post[1]!.body as FormData).get('file')).toBeInstanceOf(File)
  })

  it('shows the API message when an upload is rejected', async () => {
    mockFetch({
      'GET /api/documents': { body: [] },
      'POST /api/documents': {
        status: 409,
        body: { detail: 'a.pdf differs from the indexed version' },
      },
    })
    render(<Documents />)

    await userEvent.upload(
      screen.getByLabelText(/Drop PDF or Markdown/),
      new File(['x'], 'a.pdf', { type: 'application/pdf' }),
    )

    expect(await screen.findByText('a.pdf differs from the indexed version')).toHaveClass(
      'error-text',
    )
  })

  it('asks before deleting, warns about rebuilt entities and can be cancelled', async () => {
    const fetch = mockFetch({ 'GET /api/documents': { body: [row('paper')] } })
    render(<Documents />)

    await userEvent.click(await screen.findByRole('button', { name: 'Delete paper' }))
    expect(screen.getByText(/drops manual graph edits/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'No' }))

    expect(fetch.mock.calls.every(([, init]) => init?.method !== 'DELETE')).toBe(true)
    expect(screen.getByRole('button', { name: 'Delete paper' })).toBeInTheDocument()
  })

  it('deletes a document and polls until it is gone', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    let listed: DocumentRow[] = [row('paper')]
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') {
        listed = [row('paper', { status: 'deleting' })]
        return Response.json({ book: 'paper', status: 'deleting', job: null }, { status: 202 })
      }
      return Response.json(listed)
    })
    vi.stubGlobal('fetch', fetch)
    render(<Documents />)

    await user.click(await screen.findByRole('button', { name: 'Delete paper' }))
    await user.click(screen.getByRole('button', { name: 'Yes' }))

    expect(fetch).toHaveBeenCalledWith('/api/documents/paper', { method: 'DELETE' })
    expect(await screen.findByText('deleting')).toHaveClass('status-deleting')
    expect(screen.queryByRole('button', { name: 'Delete paper' })).not.toBeInTheDocument()

    listed = []
    await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(screen.getByText(/No documents yet/)).toBeInTheDocument()
  })

  it('shows why a delete was refused', async () => {
    mockFetch({
      'GET /api/documents': { body: [row('paper')] },
      'DELETE /api/documents/paper': {
        status: 409,
        body: { detail: 'paper has a queued or running job; wait for it to finish' },
      },
    })
    render(<Documents />)

    await userEvent.click(await screen.findByRole('button', { name: 'Delete paper' }))
    await userEvent.click(screen.getByRole('button', { name: 'Yes' }))

    expect(await screen.findByText(/wait for it to finish/)).toHaveClass('error-box')
  })

  it('indexes a repository, follows its job and shows the summary', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const job = {
      id: 41,
      kind: 'index_repo',
      payload: {},
      status: 'queued' as const,
      result: null,
      error: null,
      created_at: '2026-10-31T10:00:00Z',
      started_at: null,
      finished_at: null,
    }
    const fetch = mockFetch({
      'GET /api/documents': { body: [] },
      'POST /api/repos': {
        status: 202,
        body: { repo: 'https://github.com/org/handbook', job },
      },
      'GET /api/jobs/41': {
        body: {
          ...job,
          status: 'done',
          result: { files: 3, indexed: ['a', 'b'], unchanged: 1, deleted: [] },
        },
      },
    })
    render(<Documents />)

    await user.type(
      await screen.findByLabelText('Index a Git repository'),
      'https://github.com/org/handbook',
    )
    await user.type(screen.getByLabelText('Branch or tag'), 'v2')
    await user.click(screen.getByRole('button', { name: 'Index' }))

    const call = fetch.mock.calls.find(([url]) => url === '/api/repos')
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      url: 'https://github.com/org/handbook',
      ref: 'v2',
    })
    const repos = screen.getByRole('list', { name: 'Repositories' })
    expect(repos).toHaveTextContent('github.com/org/handbook job 41 queued')

    await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(repos).toHaveTextContent('done: 3 files, 2 indexed, 1 unchanged, 0 deleted')
  })

  it('shows why a repository was refused', async () => {
    mockFetch({
      'GET /api/documents': { body: [] },
      'POST /api/repos': {
        status: 422,
        body: { detail: "Host '10.0.0.5' is not in REPO_HOSTS (github.com)" },
      },
    })
    render(<Documents />)

    await userEvent.type(
      await screen.findByLabelText('Index a Git repository'),
      'https://10.0.0.5/org/x',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Index' }))

    expect(await screen.findByText(/not in REPO_HOSTS/)).toHaveClass('error-box')
  })

  it('sends the access token of a private repository and clears it', async () => {
    const fetch = mockFetch({
      'GET /api/documents': { body: [] },
      'POST /api/repos': {
        status: 202,
        body: {
          repo: 'https://github.com/org/private',
          job: {
            id: 42,
            kind: 'index_repo',
            payload: {},
            status: 'queued',
            result: null,
            error: null,
            created_at: '2026-11-08T10:00:00Z',
            started_at: null,
            finished_at: null,
          },
        },
      },
    })
    render(<Documents />)

    await userEvent.type(
      await screen.findByLabelText('Index a Git repository'),
      'https://github.com/org/private',
    )
    const token = screen.getByLabelText('Access token')
    expect(token).toHaveAttribute('type', 'password')
    await userEvent.type(token, 'github_pat_11ABC')
    await userEvent.click(screen.getByRole('button', { name: 'Index' }))

    const call = fetch.mock.calls.find(([url]) => url === '/api/repos')
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      url: 'https://github.com/org/private',
      token: 'github_pat_11ABC',
    })
    expect(await screen.findByLabelText('Access token')).toHaveValue('')
  })

  it('labels documents from a repository', async () => {
    mockFetch({
      'GET /api/documents': {
        body: [row('org-handbook--README', { repo: 'https://github.com/org/handbook' })],
      },
    })
    render(<Documents />)

    expect(await screen.findByText('from github.com/org/handbook')).toBeInTheDocument()
  })
})

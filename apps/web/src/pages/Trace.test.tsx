import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { QueryRecord, QuerySummary } from '../api'
import { mockFetch } from '../test/mockFetch'
import { TraceDetail, TraceList } from './Trace'

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/trace" element={<TraceList />} />
        <Route path="/trace/:id" element={<TraceDetail />} />
      </Routes>
    </MemoryRouter>,
  )
}

const summary = (id: number, changes: Partial<QuerySummary> = {}): QuerySummary => ({
  id,
  source: 'api',
  question: `Question ${id}?`,
  mode: 'mix',
  found: true,
  query_secs: 4.8,
  error: null,
  created_at: '2026-10-26T07:05:00Z',
  ...changes,
})

const record: QueryRecord = {
  ...summary(23),
  result: {
    question: 'Question 23?',
    answer: 'Mira Okafor [1].',
    sources: [{ ref: 1, title: 'Harbor', section: '-', pages: '', citation: 'Harbor | - | ' }],
    chunks: [{ ref: 1, chunk_id: 'chunk-abc', text: 'Mira Okafor designed the protocol.' }],
    entities: [{ name: 'Lighthouse Protocol', type: 'artifact', description: 'A protocol.' }],
    relations: [
      { source: 'Mira Okafor', target: 'Lighthouse Protocol', keywords: 'designed', description: 'd' },
    ],
    found: true,
    query_secs: 4.8,
    mode: 'mix',
  },
}

describe('Trace', () => {
  it('lists queries with their outcome', async () => {
    mockFetch({
      'GET /api/queries?limit=50': {
        body: [summary(3), summary(2, { found: false }), summary(1, { error: 'boom', found: null })],
      },
    })

    renderAt('/trace')

    expect(await screen.findByRole('link', { name: '#3' })).toHaveAttribute('href', '/trace/3')
    expect(screen.getByText('found')).toBeInTheDocument()
    expect(screen.getByText('no context')).toBeInTheDocument()
    expect(screen.getByText('error')).toBeInTheDocument()
  })

  it('shows chunks, entities and relations of one query', async () => {
    mockFetch({ 'GET /api/queries/23': { body: record } })

    renderAt('/trace/23')

    expect(await screen.findByText('Mira Okafor designed the protocol.')).toBeInTheDocument()
    expect(screen.getByText('chunk-abc')).toBeInTheDocument()
    expect(screen.getAllByText('Lighthouse Protocol').length).toBeGreaterThan(0)
    // Names open the Annotations page with a note on that entity prefilled.
    expect(screen.getByRole('link', { name: 'Mira Okafor' })).toHaveAttribute(
      'href',
      '/annotations?target_kind=entity&target_ref=Mira%20Okafor',
    )
    expect(screen.getAllByRole('link', { name: 'Lighthouse Protocol' })[0]).toHaveAttribute(
      'href',
      '/annotations?target_kind=entity&target_ref=Lighthouse%20Protocol',
    )
    expect(screen.getByText('Chunks (1)')).toBeInTheDocument()
  })

  it('shows the router decision when the answer carries one', async () => {
    const route = {
      route: 'graph_flow',
      targets: ['code'],
      method: 'rule',
      rules: ['http_route', 'flow_phrase'],
      confidence: null,
    }
    mockFetch({
      'GET /api/queries/23': { body: { ...record, result: { ...record.result!, extra: { route } } } },
    })

    renderAt('/trace/23')

    expect(
      await screen.findByText('route graph_flow → code (rule: http_route, flow_phrase)'),
    ).toBeInTheDocument()
  })

  it('shows the stored error of a failed query', async () => {
    mockFetch({
      'GET /api/queries/9': {
        body: { ...summary(9, { error: 'Connection error.', found: null }), result: null },
      },
    })

    renderAt('/trace/9')

    expect(await screen.findByText('Connection error.')).toHaveClass('error-box')
  })

  it('says when a query does not exist', async () => {
    mockFetch({})

    renderAt('/trace/404')

    expect(await screen.findByText('Query #404 not found.')).toBeInTheDocument()
  })

  it('ignores a late response for a query the user already left', async () => {
    let finish5!: (response: Response) => void
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        url === '/api/queries/5'
          ? new Promise<Response>((resolve) => (finish5 = resolve))
          : Promise.resolve(Response.json({ ...record, id: 6, question: 'Question 6?' })),
      ),
    )
    render(
      <MemoryRouter initialEntries={['/trace/5']}>
        <Link to="/trace/6">next</Link>
        <Routes>
          <Route path="/trace/:id" element={<TraceDetail />} />
        </Routes>
      </MemoryRouter>,
    )

    await userEvent.click(screen.getByRole('link', { name: 'next' }))
    expect(await screen.findByRole('heading', { name: 'Question 6?' })).toBeInTheDocument()
    await act(async () => finish5(Response.json({ ...record, id: 5, question: 'Question 5?' })))

    expect(screen.getByRole('heading', { name: 'Question 6?' })).toBeInTheDocument()
  })

  it('rejects a non-numeric id without calling the API', () => {
    const fetch = mockFetch({})

    renderAt('/trace/abc')

    expect(screen.getByText('Query #abc not found.')).toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })
})

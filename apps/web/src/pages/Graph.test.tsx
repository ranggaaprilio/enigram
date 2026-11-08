import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { GraphSearch, Job } from '../api'
import { mockFetch } from '../test/mockFetch'
import Graph from './Graph'

const SEARCH = 'GET /api/graph/search?q=60%25&limit=50'

const found: GraphSearch = {
  entities: [
    { name: 'Mutation Score', type: 'concept', description: 'below 60% blocks a release' },
    { name: 'Kestrel', type: 'organization', description: 'gates on 60%' },
  ],
  relations: [
    { source: 'Kestrel', target: 'Mutation Score', keywords: 'gate', description: 'uses 60%' },
  ],
}

const job = (
  id: number,
  kind: string,
  status: Job['status'],
  error: string | null = null,
): Job => ({
  id,
  kind,
  payload: {},
  status,
  result: null,
  error,
  created_at: '2026-10-31T10:00:00Z',
  started_at: null,
  finished_at: null,
})

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/graph" element={<Graph />} />
      </Routes>
    </MemoryRouter>,
  )
}

function bodyOf(fetch: ReturnType<typeof mockFetch>, key: string) {
  const call = fetch.mock.calls.find(([url, init]) => `${init?.method ?? 'GET'} ${url}` === key)
  return call && JSON.parse(String(call[1]?.body))
}

describe('Graph', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('searches the phrase from the URL and lists entities and relations', async () => {
    mockFetch({ [SEARCH]: { body: found } })

    renderAt('/graph?q=60%25')

    expect(await screen.findByText('below 60% blocks a release')).toBeInTheDocument()
    expect(screen.getByText('Entities (2)')).toBeInTheDocument()
    expect(screen.getByText('Kestrel → Mutation Score')).toBeInTheDocument()
    expect(screen.getByLabelText('Find entities and relations containing')).toHaveValue('60%')
  })

  it('says when nothing matches', async () => {
    mockFetch({ 'GET /api/graph/search?q=zzz&limit=50': { body: { entities: [], relations: [] } } })

    renderAt('/graph?q=zzz')

    expect(await screen.findByText('No entity mentions “zzz”.')).toBeInTheDocument()
    expect(screen.getByText('No relation mentions “zzz”.')).toBeInTheDocument()
  })

  it('edits an entity, follows the job and searches again when it is done', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const fetch = mockFetch({
      [SEARCH]: { body: found },
      'POST /api/graph/entity/edit': {
        status: 202,
        body: { job: job(7, 'edit_entity', 'queued') },
      },
      'GET /api/jobs/7': { body: job(7, 'edit_entity', 'done') },
    })
    renderAt('/graph?q=60%25')

    await user.click(await screen.findByRole('button', { name: 'Edit Mutation Score' }))
    const description = screen.getByLabelText('Description')
    await user.clear(description)
    await user.type(description, 'below 47% blocks a release')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(bodyOf(fetch, 'POST /api/graph/entity/edit')).toEqual({
      name: 'Mutation Score',
      description: 'below 47% blocks a release',
      entity_type: 'concept',
    })
    const changes = screen.getByRole('list', { name: 'Changes' })
    expect(within(changes).getByText('queued')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit Mutation Score' })).toBeDisabled()

    await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(within(changes).getByText('done')).toBeInTheDocument()
    const searches = fetch.mock.calls.filter(([url]) => String(url).startsWith('/api/graph/search'))
    expect(searches).toHaveLength(2)
  })

  it('shows why a job failed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    mockFetch({
      [SEARCH]: { body: found },
      'DELETE /api/graph/entity/Kestrel': {
        status: 202,
        body: { job: job(8, 'delete_entity', 'queued') },
      },
      'GET /api/jobs/8': { body: job(8, 'delete_entity', 'failed', "No entity 'Kestrel'") },
    })
    renderAt('/graph?q=60%25')

    await user.click(await screen.findByRole('button', { name: 'Delete Kestrel' }))
    await user.click(screen.getByRole('button', { name: 'Yes' }))
    await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(screen.getByText("No entity 'Kestrel'")).toBeInTheDocument()
  })

  it('asks before deleting and can be cancelled', async () => {
    const fetch = mockFetch({ [SEARCH]: { body: found } })
    renderAt('/graph?q=60%25')

    await userEvent.click(await screen.findByRole('button', { name: 'Delete Mutation Score' }))
    expect(screen.getByText('Delete Mutation Score?')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'No' }))

    expect(fetch.mock.calls.every(([, init]) => init?.method !== 'DELETE')).toBe(true)
  })

  it('deletes a relation by both ends', async () => {
    const fetch = mockFetch({
      [SEARCH]: { body: found },
      'DELETE /api/graph/relation?source=Kestrel&target=Mutation%20Score': {
        status: 202,
        body: { job: job(9, 'delete_relation', 'queued') },
      },
    })
    renderAt('/graph?q=60%25')

    await userEvent.click(
      await screen.findByRole('button', { name: 'Delete Kestrel → Mutation Score' }),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Yes' }))

    expect(await screen.findByText('Delete Kestrel → Mutation Score')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith(
      '/api/graph/relation?source=Kestrel&target=Mutation%20Score',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('merges the selected entities into the chosen name', async () => {
    const fetch = mockFetch({
      [SEARCH]: { body: found },
      'POST /api/graph/entity/merge': {
        status: 202,
        body: { job: job(10, 'merge_entities', 'queued') },
      },
    })
    renderAt('/graph?q=60%25')

    await userEvent.click(await screen.findByLabelText('Select Kestrel'))
    const target = screen.getByLabelText('Merge selected into')
    expect(target).toHaveValue('Kestrel')
    // Only the target is selected: nothing to merge into it yet.
    expect(screen.getByRole('button', { name: 'Merge' })).toBeDisabled()

    await userEvent.clear(target)
    await userEvent.type(target, 'Kestrel Labs')
    await userEvent.click(screen.getByRole('button', { name: 'Merge' }))

    expect(bodyOf(fetch, 'POST /api/graph/entity/merge')).toEqual({
      sources: ['Kestrel'],
      target: 'Kestrel Labs',
    })
  })
})

import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { GraphEntity, GraphSearch, Job } from '../api'
import { mockFetch } from '../test/mockFetch'
import Graph from './Graph'

const SEARCH = 'GET /api/graph/search?q=60%25&limit=50&offset=0'
const ALL = 'GET /api/graph/search?limit=50&offset=0'
const TYPES = 'GET /api/graph/types'
const RELATIONS = 'GET /api/graph/relations?entity=Mutation%20Score'

const found: GraphSearch = {
  entities: [
    {
      name: 'Mutation Score',
      type: 'concept',
      description: 'below 60% blocks a release',
      degree: 1,
    },
    { name: 'Kestrel', type: 'organization', description: 'gates on 60%', degree: 1 },
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

  // Every route the page reads on load, plus the ones a test adds.
  const routes = (extra: Parameters<typeof mockFetch>[0] = {}) =>
    mockFetch({
      [SEARCH]: { body: found },
      [ALL]: { body: found },
      [TYPES]: {
        body: [
          { type: 'concept', count: 1 },
          { type: 'organization', count: 1 },
        ],
      },
      [RELATIONS]: { body: found.relations },
      ...extra,
    })

  async function openPanel(name: string) {
    await userEvent.click(await screen.findByRole('button', { name: `Open ${name}` }))
    return screen.findByRole('dialog')
  }

  it('lists the graph before any search, most connected first', async () => {
    const fetch = routes()

    renderAt('/graph')

    expect(await screen.findByText('below 60% blocks a release')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Entities (2)' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(fetch).toHaveBeenCalledWith('/api/graph/search?limit=50&offset=0', expect.anything())
    expect(await screen.findByRole('option', { name: 'concept (1)' })).toBeInTheDocument()
  })

  it('says how to build the graph when the workspace has none', async () => {
    routes({ [ALL]: { body: { entities: [], relations: [] } } })

    renderAt('/graph')

    expect(await screen.findByText('No graph yet. Upload documents to build it.')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Upload documents' })).toBeInTheDocument()
  })

  it('searches the phrase from the URL', async () => {
    routes()

    renderAt('/graph?q=60%25')

    expect(await screen.findByText('below 60% blocks a release')).toBeInTheDocument()
    expect(screen.getByLabelText('Find entities and relations containing')).toHaveValue('60%')
  })

  it('searches while typing, once typing stops', async () => {
    const fetch = routes({ 'GET /api/graph/search?q=Kes&limit=50&offset=0': { body: found } })
    renderAt('/graph')
    await screen.findByText('below 60% blocks a release')

    await userEvent.type(screen.getByLabelText('Find entities and relations containing'), 'Kes')

    await vi.waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        '/api/graph/search?q=Kes&limit=50&offset=0',
        expect.anything(),
      ),
    )
    const typed = fetch.mock.calls.filter(([url]) => String(url).includes('q=K'))
    expect(typed).toHaveLength(1) // not one search per letter
  })

  it('filters entities by type', async () => {
    const fetch = routes({
      'GET /api/graph/search?type=concept&limit=50&offset=0': {
        body: { entities: [found.entities[0]], relations: [] },
      },
    })
    renderAt('/graph')
    await screen.findByRole('option', { name: 'concept (1)' })

    await userEvent.selectOptions(screen.getByLabelText('Entity type'), 'concept')

    await vi.waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        '/api/graph/search?type=concept&limit=50&offset=0',
        expect.anything(),
      ),
    )
    expect(await screen.findByRole('tab', { name: 'Entities (1)' })).toBeInTheDocument()
  })

  it('says when nothing matches', async () => {
    routes({
      'GET /api/graph/search?q=zzz&limit=50&offset=0': { body: { entities: [], relations: [] } },
    })

    renderAt('/graph?q=zzz')

    expect(await screen.findByText('No entity mentions “zzz”.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Relations (0)' }))
    expect(screen.getByText('No relation mentions “zzz”.')).toBeInTheDocument()
  })

  it('loads the next page on request', async () => {
    const page = Array.from({ length: 50 }, (_, i): GraphEntity => ({
      name: `E${i}`,
      type: 'concept',
      description: null,
      degree: 0,
    }))
    const last: GraphEntity = { name: 'Last', type: 'concept', description: null, degree: 0 }
    routes({
      [ALL]: { body: { entities: page, relations: [] } },
      'GET /api/graph/search?limit=50&offset=50': { body: { entities: [last], relations: [] } },
    })
    renderAt('/graph')

    expect(await screen.findByRole('tab', { name: 'Entities (50+)' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))

    expect(await screen.findByText('Last')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Entities (51)' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
  })

  it('opens an entity with its relations and jumps to the other end', async () => {
    routes({ 'GET /api/graph/relations?entity=Kestrel': { body: found.relations } })
    renderAt('/graph')

    const panel = await openPanel('Mutation Score')

    expect(within(panel).getByText('below 60% blocks a release')).toBeInTheDocument()
    const related = await within(panel).findByRole('list', { name: 'Relations' })
    await userEvent.click(within(related).getByRole('button', { name: 'Kestrel' }))

    expect(await screen.findByRole('heading', { name: 'Kestrel' })).toBeInTheDocument()
  })

  it('edits an entity from its panel, follows the job and searches again when done', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const fetch = routes({
      'POST /api/graph/entity/edit': {
        status: 202,
        body: { job: job(7, 'edit_entity', 'queued') },
      },
      'GET /api/jobs/7': { body: job(7, 'edit_entity', 'done') },
    })
    renderAt('/graph?q=60%25')

    await user.click(await screen.findByRole('button', { name: 'Open Mutation Score' }))
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
    const changes = screen.getByRole('list', { name: 'Changes', hidden: true })
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
    routes({
      'GET /api/graph/relations?entity=Kestrel': { body: [] },
      'DELETE /api/graph/entity/Kestrel': {
        status: 202,
        body: { job: job(8, 'delete_entity', 'queued') },
      },
      'GET /api/jobs/8': { body: job(8, 'delete_entity', 'failed', "No entity 'Kestrel'") },
    })
    renderAt('/graph?q=60%25')

    await user.click(await screen.findByRole('button', { name: 'Open Kestrel' }))
    await user.click(await screen.findByRole('button', { name: 'Delete Kestrel' }))
    await user.click(screen.getByRole('button', { name: 'Yes' }))
    await act(() => vi.advanceTimersByTimeAsync(3000))

    expect(screen.getByText("No entity 'Kestrel'")).toBeInTheDocument()
  })

  it('asks before deleting and can be cancelled', async () => {
    const fetch = routes()
    renderAt('/graph?q=60%25')

    const panel = await openPanel('Mutation Score')
    await userEvent.click(within(panel).getByRole('button', { name: 'Delete Mutation Score' }))
    expect(within(panel).getByText('Delete Mutation Score?')).toBeInTheDocument()
    await userEvent.click(within(panel).getByRole('button', { name: 'No' }))

    expect(fetch.mock.calls.every(([, init]) => init?.method !== 'DELETE')).toBe(true)
  })

  it('deletes a relation by both ends from the relations tab', async () => {
    const fetch = routes({
      'DELETE /api/graph/relation?source=Kestrel&target=Mutation%20Score': {
        status: 202,
        body: { job: job(9, 'delete_relation', 'queued') },
      },
    })
    renderAt('/graph?q=60%25')

    await userEvent.click(await screen.findByRole('tab', { name: 'Relations (1)' }))
    const panel = await openPanel('Kestrel → Mutation Score')
    await userEvent.click(
      within(panel).getByRole('button', { name: 'Delete Kestrel → Mutation Score' }),
    )
    await userEvent.click(within(panel).getByRole('button', { name: 'Yes' }))

    expect(await screen.findByText('Delete Kestrel → Mutation Score')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith(
      '/api/graph/relation?source=Kestrel&target=Mutation%20Score',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('merges the selected entities into the chosen name', async () => {
    const fetch = routes({
      'POST /api/graph/entity/merge': {
        status: 202,
        body: { job: job(10, 'merge_entities', 'queued') },
      },
    })
    renderAt('/graph?q=60%25')

    await userEvent.click(await screen.findByLabelText('Select Kestrel'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument() // ticking does not open the row
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

import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { ReactElement } from 'react'
import { accessOf, AccessProvider } from '../access'
import type { Stats, WorkspaceStats } from '../api'
import { asRole } from '../test/access'
import { REPO } from '../test/fixtures'
import { mockFetch } from '../test/mockFetch'
import Dashboard from './Dashboard'

function stats(days = 30): Stats {
  return {
    workspace: 'default',
    days,
    since: '2026-11-01',
    tokens: {
      budget: 1000,
      per_key_budget: 500,
      today: 250,
      left_today: 750,
      by_day: [
        { day: '2026-11-07', source: 'query', tokens: 200 },
        { day: '2026-11-07', source: 'job:index_repo', tokens: 50 },
      ],
      by_key: null,
    },
    questions: {
      asked: 8,
      unanswered: 2,
      errors: 1,
      p50_secs: 9.75,
      p95_secs: 45.4,
      by_day: [{ day: '2026-11-03', asked: 8, unanswered: 2, errors: 1 }],
    },
    answers: {
      routes: [{ targets: 'code+docs', method: 'unsure', count: 5 }],
      origins: { code: 3, unsupported: 1 },
    },
    knowledge: {
      documents: { indexed: 2 },
      repos: [
        {
          id: 4,
          name: 'org-white-board',
          url: 'https://github.com/org/white-board',
          ref: null,
          indexed_sha: 'd0eebac9f3e4',
          indexed_at: '2026-11-07T13:17:54Z',
          files: 78,
          symbols: 213,
          entrypoints: 7,
        },
      ],
    },
    review: {
      entrypoints: { entrypoints: 14, reviewed: 3, current: 2 },
      notes: [
        { note_type: 'technical', status: 'verified', count: 6 },
        { note_type: 'technical', status: 'rejected', count: 4 },
      ],
    },
    attention: {
      proposed: 3,
      needs_verification: 1,
      orphaned: 0,
      note_conflicts: 1,
      doc_conflicts: 42,
      failed_jobs: 2,
      budget_exceeded_jobs: 0,
      jobs_since_days: 7,
    },
  }
}

// An admin of "default" unless `ui` wraps it otherwise; the owner's table has its own test.
function renderPage(
  routes: Parameters<typeof mockFetch>[0],
  url = '/dashboard',
  wrap = (ui: ReactElement) => asRole('admin', ui),
) {
  const fetch = mockFetch({ 'GET /api/stats/repos': { body: [REPO] }, ...routes })
  render(
    wrap(
      <MemoryRouter initialEntries={[url]}>
        <Dashboard />
      </MemoryRouter>,
    ),
  )
  return fetch
}

const panel = (name: string) => screen.getByRole('region', { name })

describe('Dashboard', () => {
  it('shows what needs a person first and links each number to its page', async () => {
    renderPage({ 'GET /api/stats?days=30': { body: stats() } })

    expect(await screen.findByRole('heading', { name: 'Needs attention' })).toBeInTheDocument()
    const order = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(order).toEqual([
      'Needs attention',
      'Questions',
      'Answer quality',
      'Review coverage',
      'Knowledge',
      'Token usage',
    ])
    const attention = panel('Needs attention')
    const link = (name: RegExp) => within(attention).getByRole('link', { name })
    expect(link(/Proposals\s*3/)).toHaveAttribute('href', '/review?show=proposed')
    expect(link(/Re-check\s*1/)).toHaveAttribute('href', '/review?show=needs_verification')
    expect(link(/Orphaned or conflicting\s*1/)).toHaveAttribute('href', '/review?show=conflict')
    expect(link(/Failed jobs\s*2/)).toHaveAttribute('href', '/documents')
    expect(within(attention).getByText(/42 places where a document/)).toBeInTheDocument()

    const questions = panel('Questions')
    expect(within(questions).getByText('25%')).toBeInTheDocument() // 2 of 8 not answered
    expect(within(questions).getByText('9.8 s')).toBeInTheDocument()
    expect(
      within(questions).getByRole('listitem', { name: '2026-11-03: 8 questions' }),
    ).toBeInTheDocument()
    expect(within(questions).getAllByRole('listitem')).toHaveLength(30) // quiet days included

    expect(within(panel('Review coverage')).getByText('14%')).toBeInTheDocument() // 2 of 14
    const knowledge = panel('Knowledge')
    expect(await within(knowledge).findByRole('link', { name: 'org-white-board' })).toHaveAttribute(
      'href',
      '/dashboard/repos/4',
    )
    expect(within(knowledge).getByText('Behind')).toBeInTheDocument()
    expect(within(knowledge).getByText('Polling updates')).toBeInTheDocument()
    expect(within(panel('Token usage')).getByText('750')).toBeInTheDocument()
  })

  it('reads a day on hover', async () => {
    renderPage({ 'GET /api/stats?days=30': { body: stats() } })
    const tokens = await screen.findByRole('region', { name: 'Token usage' })
    expect(within(tokens).getByText('250 tokens in 30 days')).toBeInTheDocument()
    await userEvent.hover(within(tokens).getByRole('listitem', { name: '2026-11-07: 250 tokens' }))
    expect(
      within(tokens).getByText('2026-11-07: 250 tokens, query 200, job:index_repo 50'),
    ).toBeInTheDocument()
  })

  it('reads the days from the keyboard with one Tab stop', async () => {
    renderPage({ 'GET /api/stats?days=30': { body: stats() } })
    const tokens = await screen.findByRole('region', { name: 'Token usage' })
    const chart = within(tokens).getByRole('list', { name: /Tokens per day/ })
    act(() => chart.focus())
    expect(within(tokens).getByText('2026-11-30: 0 tokens')).toBeInTheDocument() // the last day
    await userEvent.keyboard('{Home}')
    expect(within(tokens).getByText('2026-11-01: 0 tokens')).toBeInTheDocument()
    for (let i = 0; i < 6; i++) await userEvent.keyboard('{ArrowRight}')
    expect(
      within(tokens).getByText('2026-11-07: 250 tokens, query 200, job:index_repo 50'),
    ).toBeInTheDocument()
    expect(
      within(tokens)
        .queryAllByRole('listitem')
        .filter((d) => d.tabIndex >= 0),
    ).toEqual([])
  })

  it('keeps the period in the URL', async () => {
    const fetch = renderPage(
      {
        'GET /api/stats?days=30': { body: stats() },
        'GET /api/stats?days=7': { body: stats(7) },
      },
      '/dashboard?days=7',
    )
    expect(await screen.findByRole('button', { name: '7 days' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await userEvent.click(screen.getByRole('button', { name: '30 days' }))
    expect(await screen.findByRole('button', { name: '30 days' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    const periods = fetch.mock.calls.map(([url]) => url).filter((url) => url.includes('days='))
    expect(periods).toEqual(['/api/stats?days=7', '/api/stats?days=30'])
  })

  it('lists tokens per key when the API sends them (admins only)', async () => {
    const base = stats()
    renderPage({
      'GET /api/stats?days=30': {
        body: {
          ...base,
          tokens: {
            ...base.tokens,
            by_key: [
              { day: '2026-11-07', api_key: 'web', tokens: 200 },
              { day: '2026-11-07', api_key: '', tokens: 50 },
            ],
          },
        },
      },
    })
    expect(await screen.findByText('Key (admins only)')).toBeInTheDocument()
    expect(screen.getByText('worker (no key)')).toBeInTheDocument()
    expect(screen.getByText('200 / 500')).toBeInTheDocument()
  })

  it('has no key table without them', async () => {
    renderPage({ 'GET /api/stats?days=30': { body: stats() } })
    await screen.findByRole('heading', { name: 'Token usage' })
    expect(screen.queryByText('Key (admins only)')).toBeNull()
  })

  it('points a new workspace to its first step', async () => {
    const base = stats()
    renderPage({
      'GET /api/stats?days=30': {
        body: {
          ...base,
          questions: { ...base.questions, asked: 0, by_day: [] },
          knowledge: { documents: {}, repos: [] },
        },
      },
    })
    expect(
      await screen.findByRole('link', { name: 'Add documents or a repository' }),
    ).toHaveAttribute('href', '/documents')
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull()
  })

  it('shows an API error where the panels would be', async () => {
    renderPage({ 'GET /api/stats?days=30': { status: 500, body: { detail: 'database down' } } })
    expect(await screen.findByText(/database down/)).toBeInTheDocument()
  })

  it('lists every workspace against the budget for an owner', async () => {
    const owner = {
      principal: { name: 'me', kind: 'key', owner: true, default_workspace: 'default' },
      roles: { default: 'admin', 'ws-b': 'admin' },
    }
    const row = { tokens_period: 900, asked: 4, unanswered: 1, waiting: 2, failed_jobs: 0 }
    const all: WorkspaceStats = {
      days: 30,
      since: '2026-10-10',
      budget: 1000,
      jobs_since_days: 7,
      workspaces: [
        { workspace: 'default', tokens_today: 250, left_today: 750, ...row },
        { workspace: 'ws-b', tokens_today: 500, left_today: 500, ...row },
      ],
      total: {
        tokens_today: 750,
        tokens_period: 1800,
        asked: 8,
        unanswered: 2,
        waiting: 4,
        failed_jobs: 0,
      },
    }
    renderPage(
      {
        'GET /api/stats?days=30': { body: stats() },
        'GET /api/stats/workspaces?days=30': { body: all },
      },
      '/dashboard',
      (ui) => <AccessProvider value={accessOf(owner, 'default')}>{ui}</AccessProvider>,
    )
    const table = await screen.findByRole('region', { name: 'All workspaces' })
    expect(await within(table).findByRole('link', { name: 'ws-b' })).toHaveAttribute(
      'href',
      '/w/ws-b/dashboard',
    )
    expect(within(table).getByText('50% of the budget')).toBeInTheDocument()
    const totals = within(table).getByRole('row', { name: /^All/ })
    expect(within(totals).getByText('1,800')).toBeInTheDocument()
  })

  it('has no workspace table for an admin who is not an owner', async () => {
    const fetch = renderPage({ 'GET /api/stats?days=30': { body: stats() } })
    await screen.findByRole('heading', { name: 'Token usage' })
    expect(screen.queryByRole('heading', { name: 'All workspaces' })).toBeNull()
    expect(fetch.mock.calls.some(([url]) => url.includes('/stats/workspaces'))).toBe(false)
  })
})

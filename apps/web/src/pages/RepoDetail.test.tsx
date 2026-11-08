import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { Role, RepoStatDetail } from '../api'
import { asRole } from '../test/access'
import { REPO } from '../test/fixtures'
import { mockFetch, type Reply } from '../test/mockFetch'
import RepoDetail from './RepoDetail'

const DETAIL: RepoStatDetail = {
  ...REPO,
  last_run: null,
  runs: [
    {
      job_id: 12,
      status: 'failed',
      trigger: 'poll',
      created_at: '2026-11-08T08:00:00Z',
      finished_at: '2026-11-08T08:00:04Z',
      secs: 4,
      commit_sha: null,
      commit_at: null,
      tokens: null,
      files_changed: null,
      symbols_changed: null,
      staleness: null,
      error: 'git fetch failed: unreachable',
    },
    {
      job_id: 9,
      status: 'done',
      trigger: 'manual',
      created_at: '2026-11-07T13:17:51Z',
      finished_at: '2026-11-07T13:17:55Z',
      secs: 2.7,
      commit_sha: 'd0eebac9f3e4',
      commit_at: '2026-04-18T21:21:03+07:00',
      tokens: 1200,
      files_changed: 3,
      symbols_changed: 8,
      staleness: { checked: 4, unchanged: 2, flagged_hash: 2, orphaned: 0 },
      error: null,
    },
  ],
  quality: {
    adapters: { next: 7 },
    fallback: null,
    unmatched_frameworks: [],
    files_skipped: { dts: 3, test: 0 },
    calls_resolved_share: 0.541,
    calls_in_repo_share: 0.346,
    diagnostics_sample: ["app/page.tsx:3 Cannot find module '@clerk/nextjs'."],
    secrets_redacted: 0,
  },
  summaries: { flows: 7, entrypoints: 7, symbols: 0, cached: 69, capped_flows: 1 },
  staleness: { checked: 4, unchanged: 2, flagged_hash: 2, orphaned: 0 },
}

function renderAs(role: Role, routes: Record<string, Reply> = {}, id = '4') {
  const fetch = mockFetch({ 'GET /api/stats/repos/4': { body: DETAIL }, ...routes })
  render(
    asRole(
      role,
      <MemoryRouter initialEntries={[`/dashboard/repos/${id}`]}>
        <Routes>
          <Route path="/dashboard/repos/:id" element={<RepoDetail />} />
        </Routes>
      </MemoryRouter>,
    ),
  )
  return fetch
}

const section = (name: string) => screen.getByRole('region', { name })

describe('Repository detail', () => {
  it('shows its state, index history, graph quality and effect on notes', async () => {
    renderAs('viewer')
    expect(await screen.findByRole('heading', { name: 'org-white-board' })).toBeInTheDocument()
    expect(screen.getByText('Behind')).toBeInTheDocument()
    expect(screen.getByText(/The remote is at ffffff/)).toBeInTheDocument()
    expect(screen.getByText('Polling')).toBeInTheDocument()

    const history = section('Index history')
    expect(within(history).getAllByRole('row')).toHaveLength(3) // header + 2 runs
    expect(within(history).getByText('git fetch failed: unreachable')).toBeInTheDocument()
    expect(within(history).getByText('3 files · 8 symbols')).toBeInTheDocument()

    expect(within(section('Graph quality')).getByText('next (7 entry points)')).toBeInTheDocument()
    expect(within(section('Graph quality')).getByText('54%')).toBeInTheDocument()
    expect(within(section('Summaries')).getByText('7 of 7 (1 cut short)')).toBeInTheDocument()
    const notes = section('Effect on notes, last run')
    expect(within(notes).getByText('Flagged: their code changed')).toBeInTheDocument()
    expect(within(notes).getByRole('link', { name: 'Open the Inbox' })).toHaveAttribute(
      'href',
      '/review',
    )
    // A viewer reads; re-indexing is a maintainer's.
    expect(screen.queryByRole('button', { name: /Re-index now/ })).toBeNull()
    expect(screen.getByRole('link', { name: 'Open in Knowledge' })).toHaveAttribute(
      'href',
      '/knowledge?repo=4',
    )
  })

  it('lets a maintainer queue a re-index of the same ref', async () => {
    const fetch = renderAs('maintainer', {
      'POST /api/repos': { status: 202, body: { repo: REPO.url, job: { id: 13 } } },
    })
    await userEvent.click(await screen.findByRole('button', { name: /Re-index now/ }))
    const post = fetch.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect(JSON.parse(String(post[1]?.body))).toEqual({ url: REPO.url })
  })

  it('says when the repository does not exist', async () => {
    renderAs('viewer', {}, '99')
    expect(await screen.findByText('Repository 99 not found.')).toBeInTheDocument()
  })
})

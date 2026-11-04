import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { QueryResult } from '../api'
import { mockFetch } from '../test/mockFetch'
import Ask from './Ask'

const result: QueryResult & { id: number } = {
  id: 5,
  question: 'Who designed the protocol?',
  answer: '**Mira Okafor** designed it [1].',
  sources: [
    {
      ref: 1,
      title: 'Harbor Signals Study',
      section: 'Methods > Design',
      pages: '3',
      citation: 'Harbor Signals Study | Methods > Design | p. 3',
    },
  ],
  chunks: [],
  entities: [],
  relations: [],
  found: true,
  query_secs: 4.8,
  mode: 'mix',
}

async function askQuestion(mode?: string) {
  render(
    <MemoryRouter>
      <Ask />
    </MemoryRouter>,
  )
  await userEvent.type(screen.getByLabelText('Question'), 'Who designed the protocol?')
  if (mode) await userEvent.selectOptions(screen.getByLabelText('Mode'), mode)
  await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
}

describe('Ask', () => {
  it('sends the question and renders the answer, sources and trace link', async () => {
    const fetch = mockFetch({ 'POST /api/query': { body: result } })

    await askQuestion()

    expect((await screen.findByText('Mira Okafor')).tagName).toBe('STRONG')
    expect(screen.getByText(/Harbor Signals Study · Methods > Design · p\. 3/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View trace' })).toHaveAttribute('href', '/trace/5')
    // Auto is the default: the router picks the documents, the code or both.
    expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({
      question: 'Who designed the protocol?',
      mode: 'mix',
      scope: 'auto',
    })
  })

  it.each(['mix', 'naive'])('sends %s to the documents explicitly', async (mode) => {
    const fetch = mockFetch({ 'POST /api/query': { body: { ...result, mode } } })

    await askQuestion(mode)

    await screen.findByText('Mira Okafor')
    expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({
      question: 'Who designed the protocol?',
      mode,
      scope: 'docs',
    })
  })

  it('asks the code agent', async () => {
    const fetch = mockFetch({
      'POST /api/query': { body: { ...result, mode: 'enigram-agent', sources: [] } },
    })

    await askQuestion('code')

    await screen.findByText('Mira Okafor')
    expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({
      question: 'Who designed the protocol?',
      mode: 'mix',
      scope: 'code',
    })
  })

  it('says when the index had no matching context', async () => {
    mockFetch({ 'POST /api/query': { body: { ...result, found: false, sources: [] } } })

    await askQuestion()

    expect(await screen.findByText(/do not answer this question/)).toBeInTheDocument()
  })

  it('shows the API error', async () => {
    mockFetch({
      'POST /api/query': { status: 502, body: { detail: 'Query failed: Connection error.' } },
    })

    await askQuestion()

    expect(await screen.findByText('Query failed: Connection error.')).toHaveClass('error-box')
  })

  it('cannot submit an empty question', () => {
    render(
      <MemoryRouter>
        <Ask />
      </MemoryRouter>,
    )

    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
  })
})

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { QueryResult } from '../api'
import Answer, { withMarks } from './Answer'

function result(answer: string): QueryResult {
  return {
    question: 'q',
    answer,
    sources: [],
    chunks: [],
    entities: [],
    relations: [],
    found: true,
    query_secs: 1,
    mode: 'enigram-agent',
  }
}

describe('Answer', () => {
  it('renders origin tags as badges', () => {
    const { container } = render(
      <Answer
        result={result(
          'No invite code is needed src/auth/auth.service.ts:201-233@70ae60dbb609 [code].',
        )}
      />,
    )

    const badges = container.querySelectorAll('.badge-code')
    expect(badges).toHaveLength(1)
    expect(badges[0]).toHaveTextContent('code')
    expect(container.textContent).not.toContain('[code]')
    expect(container.textContent).toContain('@70ae60dbb609')
  })

  it('badges every origin in list items and emphasis, and keeps [n] citations', () => {
    const { container } = render(
      <Answer
        result={result(
          '- **It guesses** [unsupported].\n- The paper says so [1] [document][human].',
        )}
      />,
    )

    expect(container.querySelectorAll('.badge-unsupported')).toHaveLength(1)
    expect(container.querySelectorAll('.badge-document')).toHaveLength(1)
    expect(container.querySelectorAll('.badge-human')).toHaveLength(1)
    expect(screen.getByText(/The paper says so \[1\]/)).toBeInTheDocument()
  })

  it('marks the verdict after the origin badges and counts the sentences', () => {
    const { container } = render(
      <Answer
        result={{
          ...result(
            'It checks the hash [1] [document]. It guesses [unsupported]. Both [1] [code][document].',
          ),
          verification: {
            marks: ['verified', null, 'unverified'],
            verified: 1,
            checked: 2,
            unsupported: 1,
          },
        }}
      />,
    )

    expect(container.querySelectorAll('.verdict-verified')).toHaveLength(1)
    expect(container.querySelectorAll('.verdict-unverified')).toHaveLength(1)
    expect(container.querySelector('.badge-document + .verdict-verified')).not.toBeNull()
    expect(container.textContent).not.toContain('[=')
    expect(screen.getByText(/Verified 1 of 2 cited sentences · 1 without a source/)).toBeVisible()
  })

  it('shows no verdict without a view', () => {
    const { container } = render(<Answer result={result('It checks the hash [1] [document].')} />)

    expect(container.querySelector('.verdict')).toBeNull()
    expect(screen.queryByText(/cited sentences/)).toBeNull()
  })
})

describe('withMarks', () => {
  it('skips code fences, headings and the references', () => {
    const answer = [
      '## Heading [code]',
      'One [code]. Two [document].',
      '```',
      'not this [code]',
      '```',
      '### References',
      '- [1] Handbook [document]',
    ].join('\n')

    expect(withMarks(answer, ['verified', 'unverified'])).toContain(
      'One [code][=verified]. Two [document][=unverified].',
    )
    expect(withMarks(answer, ['verified', 'unverified'])).toContain('not this [code]\n')
  })

  it('marks nothing when the marks do not fit the tags', () => {
    const answer = 'One [code]. Two [document].'

    expect(withMarks(answer, ['verified'])).toBe(answer)
    expect(withMarks(answer, ['verified', null, 'verified'])).toBe(answer)
  })
})

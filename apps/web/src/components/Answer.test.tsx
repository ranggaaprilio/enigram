import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { QueryResult } from '../api'
import Answer from './Answer'

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
})

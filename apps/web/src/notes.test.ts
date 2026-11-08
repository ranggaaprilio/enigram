import { describe, expect, it } from 'vitest'
import { citeLine, SIMILAR, similarity } from './notes'

describe('similarity', () => {
  it('scores shared words and ignores case, punctuation and short words', () => {
    expect(similarity('Logout revokes refresh tokens.', 'logout, REVOKES refresh tokens')).toBe(1)
    expect(similarity('Logout revokes the refresh token.', 'logout revokes refresh token')).toBe(
      0.8,
    )
    expect(similarity('Only admins can export users', 'Exports are slow on Mondays')).toBeLessThan(
      SIMILAR,
    )
    expect(similarity('', 'anything')).toBe(0)
  })
})

describe('citeLine', () => {
  it('writes the line answers cite', () => {
    const day = new Date('2026-11-08T10:00:00Z')
    expect(citeLine('Only  admins.\n', 'Ann Lee', day)).toBe(
      'note#… [verified by Ann Lee 2026-11-08] "Only admins."',
    )
    expect(citeLine('Times out.', null, day)).toBe('note#… [proposed] "Times out."')
  })
})

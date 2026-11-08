import { describe, expect, it } from 'vitest'
import { patchMessage } from './toasts'

describe('patchMessage', () => {
  it.each([
    [{ status: 'verified' as const }, 'Note verified'],
    [{ status: 'verified' as const, conflict: true }, 'Marked: the code is wrong now'],
    [{ status: 'rejected' as const }, 'Note rejected'],
    [{ status: 'needs_verification' as const }, 'Note flagged for a re-check'],
    [{ target_ref: 'src/a.ts#A.b' }, 'Note relinked'],
    [{ conflict: true }, 'Marked as conflicting'],
    [{ conflict: false }, 'Conflict mark removed'],
    [{ text: 'x' }, 'Note saved'],
  ])('%j says %s', (body, message) => {
    expect(patchMessage(body)).toBe(message)
  })
})

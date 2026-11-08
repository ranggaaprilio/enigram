import type { AnnotationPatch } from './api'

// What a note change did, for its toast (docs/design.md, U2: a toast for the outcome of an
// action; errors stay where they happened).
export function patchMessage(body: AnnotationPatch): string {
  if (body.status === 'verified')
    return body.conflict ? 'Marked: the code is wrong now' : 'Note verified'
  if (body.status === 'rejected') return 'Note rejected'
  if (body.status === 'needs_verification') return 'Note flagged for a re-check'
  if (body.target_ref !== undefined) return 'Note relinked'
  if (body.conflict !== undefined)
    return body.conflict ? 'Marked as conflicting' : 'Conflict mark removed'
  return 'Note saved'
}

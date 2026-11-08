// Small helpers for the note form (plan item 5, A3).

const WORD = /[\p{L}\p{N}]{3,}/gu

function words(text: string): Set<string> {
  return new Set(text.toLowerCase().match(WORD) ?? [])
}

// Share of words two notes have in common (Jaccard, 0-1). At SIMILAR or above the form warns
// that the note may say what an existing one already says.
export const SIMILAR = 0.5

export function similarity(a: string, b: string): number {
  const x = words(a)
  const y = words(b)
  if (x.size === 0 || y.size === 0) return 0
  let shared = 0
  for (const w of x) if (y.has(w)) shared++
  return shared / (x.size + y.size - shared)
}

// How answers cite a note, as the API's note_line (apps/api/src/enigram/domain/annotation.py)
// writes it: `note#7 [verified by alice 2026-11-02] "text"`. A new note has no number yet.
export function citeLine(text: string, verifiedBy: string | null, day: Date): string {
  const state = verifiedBy
    ? `verified by ${verifiedBy} ${day.toISOString().slice(0, 10)}`
    : 'proposed'
  return `note#… [${state}] "${text.split(/\s+/).join(' ').trim()}"`
}

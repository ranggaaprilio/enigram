import type { Source } from './api'

const pad = (n: number) => String(n).padStart(2, '0')

export function formatTime(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`
  )
}

export function formatSecs(secs: number | null): string {
  return secs == null ? '-' : `${secs.toFixed(1)} s`
}

export function sourceLabel(source: Source): string {
  const parts = [source.title]
  if (source.section && source.section !== '-') parts.push(source.section)
  if (source.pages) parts.push(`p. ${source.pages}`)
  return parts.join(' · ')
}

// How an answer was produced (QueryResult.mode), in words rather than engine names.
export function modeLabel(mode: string): string {
  switch (mode) {
    case 'auto':
      return 'auto · documents + code'
    case 'mix':
      return 'documents · graph + vectors'
    case 'naive':
      return 'documents · vectors only'
    case 'enigram-agent':
      return 'code agent'
    default:
      return mode
  }
}

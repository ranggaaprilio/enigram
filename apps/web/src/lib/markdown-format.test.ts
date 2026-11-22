import { describe, expect, it } from 'vitest'
import { applyFormat } from './markdown-format'

// `|` marks the selection: one bar is the cursor, two bars a selected range.
function at(marked: string) {
  const start = marked.indexOf('|')
  const end = marked.indexOf('|', start + 1)
  if (end < 0) return { value: marked.replace('|', ''), start, end: start }
  return { value: marked.replace(/\|/g, ''), start, end: end - 1 }
}

function show({ value, start, end }: { value: string; start: number; end: number }) {
  return start === end
    ? `${value.slice(0, start)}|${value.slice(start)}`
    : `${value.slice(0, start)}|${value.slice(start, end)}|${value.slice(end)}`
}

function run(marked: string, kind: Parameters<typeof applyFormat>[3]) {
  const { value, start, end } = at(marked)
  return show(applyFormat(value, start, end, kind))
}

describe('applyFormat', () => {
  it('wraps the selection in bold, italic or code, keeping it selected', () => {
    expect(run('a |word| b', 'bold')).toBe('a **|word|** b')
    expect(run('a |word| b', 'italic')).toBe('a _|word|_ b')
    expect(run('a |word| b', 'code')).toBe('a `|word|` b')
  })

  it('puts the cursor between the marks when nothing is selected', () => {
    expect(run('a | b', 'bold')).toBe('a **|** b')
  })

  it('removes the marks when the selection is already wrapped', () => {
    expect(run('a **|word|** b', 'bold')).toBe('a |word| b')
    expect(run('a `|word|` b', 'code')).toBe('a |word| b')
  })

  it('fences a selection over several lines as a code block', () => {
    expect(run('|x = 1\ny = 2|', 'code')).toBe('```\n|x = 1\ny = 2|\n```')
  })

  it('makes a link of the selection and selects the address to type over', () => {
    expect(run('see |docs| here', 'link')).toBe('see [docs](|url|) here')
    expect(run('see | here', 'link')).toBe('see [|text|](url) here')
  })

  it('prefixes every selected line, and takes the prefix off when all have it', () => {
    expect(run('|one\ntwo|', 'ul')).toBe('|- one\n- two|')
    expect(run('|- one\n- two|', 'ul')).toBe('|one\ntwo|')
    expect(run('|one\ntwo|', 'ol')).toBe('|1. one\n2. two|')
    expect(run('|1. one\n2. two|', 'ol')).toBe('|one\ntwo|')
    expect(run('in a ti|tle', 'heading')).toBe('|### in a title|')
  })
})

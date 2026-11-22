// The markdown editor's toolbar actions on plain text: each takes the text and the selection and
// returns the new text and selection, so the editor stays a plain <textarea>.
export type Format = 'bold' | 'italic' | 'code' | 'link' | 'heading' | 'ul' | 'ol'

export interface Edit {
  value: string
  start: number
  end: number
}

const WRAP: Partial<Record<Format, string>> = { bold: '**', italic: '_', code: '`' }

// A note's headings sit under the page's h1/h2 and the note card: level 3.
const PREFIX: Partial<Record<Format, { add: (i: number) => string; has: RegExp }>> = {
  heading: { add: () => '### ', has: /^#{1,6} / },
  ul: { add: () => '- ', has: /^[-*] / },
  ol: { add: (i) => `${i + 1}. `, has: /^\d+\. / },
}

export function applyFormat(value: string, start: number, end: number, kind: Format): Edit {
  const selected = value.slice(start, end)
  if (kind === 'code' && selected.includes('\n')) {
    const open = '```\n'
    return {
      value: `${value.slice(0, start)}${open}${selected}\n\`\`\`${value.slice(end)}`,
      start: start + open.length,
      end: end + open.length,
    }
  }
  if (kind === 'link') {
    if (selected) {
      const head = `${value.slice(0, start)}[${selected}](`
      return { value: `${head}url)${value.slice(end)}`, start: head.length, end: head.length + 3 }
    }
    const head = `${value.slice(0, start)}[`
    return {
      value: `${head}text](url)${value.slice(end)}`,
      start: head.length,
      end: head.length + 4,
    }
  }
  const mark = WRAP[kind]
  if (mark) {
    const n = mark.length
    if (value.slice(start - n, start) === mark && value.slice(end, end + n) === mark) {
      return {
        value: value.slice(0, start - n) + selected + value.slice(end + n),
        start: start - n,
        end: end - n,
      }
    }
    return {
      value: value.slice(0, start) + mark + selected + mark + value.slice(end),
      start: start + n,
      end: end + n,
    }
  }
  const prefix = PREFIX[kind]!
  // Whole lines: from the start of the first selected line to the end of the last.
  const from = value.lastIndexOf('\n', start - 1) + 1
  const lineEnd = value.indexOf('\n', end)
  const to = lineEnd < 0 ? value.length : lineEnd
  const lines = value.slice(from, to).split('\n')
  const off = lines.every((line) => prefix.has.test(line))
  const changed = lines
    .map((line, i) =>
      off ? line.replace(prefix.has, '') : prefix.add(i) + line.replace(prefix.has, ''),
    )
    .join('\n')
  return {
    value: value.slice(0, from) + changed + value.slice(to),
    start: from,
    end: from + changed.length,
  }
}

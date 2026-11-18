import { Children, cloneElement, isValidElement, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, CircleDashed, Info } from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import type { QueryResult, VerificationView } from '../api'
import { sourceLabel } from '../format'

// The per-sentence origin tags the API appends (domain/origin.py ORIGINS), and the verdict
// marker withMarks puts after them.
const TAG = /\[(code|summary|human|document|unsupported)\]|\[=(verified|unverified)\]/g
const TAGS = /(?:\[(?:code|summary|human|document|unsupported)\])+/g
const FENCE = /^\s*(```|~~~)/
const HEADING = /^\s*#{1,6}\s/
const REFERENCES = /^\s*(#{1,6}\s*|\*\*)?references\b[\W_]*$/i

const VERDICT = {
  verified: { label: 'Verified: the cited source supports this sentence', Icon: Check },
  unverified: {
    label: 'Not verified: the cited source does not show all of this sentence',
    Icon: CircleDashed,
  },
}

// The answer with a verdict marker after the tags of each sentence that has a mark. The API
// lists the marks in the order of the tagged sentences (domain/origin.py tagged_sentences:
// not in code fences or headings, before the References heading); when the tags found here
// are not as many, the answer stays without marks rather than marking the wrong sentence.
export function withMarks(answer: string, marks: VerificationView['marks']): string {
  let n = 0
  let fence = ''
  let done = false
  const lines = answer.split('\n').map((line) => {
    if (done) return line
    if (!fence && REFERENCES.test(line)) {
      done = true
      return line
    }
    const marker = FENCE.exec(line)?.[1]
    if (marker && (!fence || marker === fence)) {
      fence = fence ? '' : marker
      return line
    }
    if (fence || HEADING.test(line)) return line
    return line.replace(TAGS, (tags) => {
      const mark = marks[n++]
      return mark ? `${tags}[=${mark}]` : tags
    })
  })
  return n === marks.length ? lines.join('\n') : answer
}

// Turns origin tags in text nodes into badges, through nested elements (emphasis, links).
function badgify(children: ReactNode): ReactNode {
  return Children.map(children, (child) => {
    if (typeof child === 'string') {
      const parts: ReactNode[] = []
      let last = 0
      for (const match of child.matchAll(TAG)) {
        const start = match.index ?? 0
        const verdict = match[2] as keyof typeof VERDICT | undefined
        parts.push(child.slice(last, start).replace(/ $/, ''))
        if (verdict) {
          const { label, Icon } = VERDICT[verdict]
          parts.push(
            <span key={start} className={`verdict verdict-${verdict}`} title={label}>
              <Icon aria-hidden />
              <span className="sr-only">{verdict}</span>
            </span>,
          )
        } else {
          parts.push(
            <span key={start} className={`badge badge-${match[1]}`} title="origin of this sentence">
              {match[1]}
            </span>,
          )
        }
        last = start + match[0].length
      }
      if (last === 0) return child
      parts.push(child.slice(last))
      return parts
    }
    if (isValidElement<{ children?: ReactNode }>(child) && child.props.children !== undefined) {
      return cloneElement(child, undefined, badgify(child.props.children))
    }
    return child
  })
}

// Answers are LLM output: react-markdown renders no raw HTML, so keep rehype-raw out.
export default function Answer({ result }: { result: QueryResult }) {
  const view = result.verification
  const answer = view ? withMarks(result.answer, view.marks) : result.answer
  return (
    <div className="mt-8">
      {!result.found && (
        <Alert variant="warning" className="mb-4">
          <Info />
          <AlertDescription>The indexed documents do not answer this question.</AlertDescription>
        </Alert>
      )}
      {/* The answer's own headings sit below this one, whatever level the model wrote. */}
      <h2 className="sr-only">Answer</h2>
      <div className="markdown">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            p: ({ children }) => <p>{badgify(children)}</p>,
            li: ({ children }) => <li>{badgify(children)}</li>,
            h1: ({ children }) => <h3>{children}</h3>,
            h2: ({ children }) => <h3>{children}</h3>,
            h3: ({ children }) => <h3>{children}</h3>,
            h4: ({ children }) => <h4>{children}</h4>,
            h5: ({ children }) => <h4>{children}</h4>,
            h6: ({ children }) => <h4>{children}</h4>,
          }}
        >
          {answer}
        </ReactMarkdown>
      </div>
      {view && (
        <p className="mt-3 mb-0 text-[13px] text-muted-foreground">
          Verified {view.verified} of {view.checked} cited sentences
          {view.unsupported > 0 && ` · ${view.unsupported} without a source`}
        </p>
      )}
      {result.sources.length > 0 && (
        <>
          <h3 className="mt-6 mb-2 font-semibold tracking-[-0.3px]">Sources</h3>
          <ul className="max-w-[72ch] divide-y border-t text-sm">
            {result.sources.map((source) => (
              <li key={source.ref} title={source.citation} className="py-2">
                [{source.ref}] {sourceLabel(source)}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

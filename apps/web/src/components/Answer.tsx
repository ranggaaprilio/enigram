import { Children, cloneElement, isValidElement, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Info } from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import type { QueryResult } from '../api'
import { sourceLabel } from '../format'

// The per-sentence origin tags the API appends (domain/origin.py ORIGINS).
const TAG = /\[(code|summary|human|document|unsupported)\]/g

// Turns origin tags in text nodes into badges, through nested elements (emphasis, links).
function badgify(children: ReactNode): ReactNode {
  return Children.map(children, (child) => {
    if (typeof child === 'string') {
      const parts: ReactNode[] = []
      let last = 0
      for (const match of child.matchAll(TAG)) {
        const start = match.index ?? 0
        parts.push(child.slice(last, start).replace(/ $/, ''))
        parts.push(
          <span key={start} className={`badge badge-${match[1]}`} title="origin of this sentence">
            {match[1]}
          </span>,
        )
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
  return (
    <div className="mt-8">
      {!result.found && (
        <Alert variant="warning" className="mb-4">
          <Info />
          <AlertDescription>The indexed documents do not answer this question.</AlertDescription>
        </Alert>
      )}
      <div className="markdown">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            p: ({ children }) => <p>{badgify(children)}</p>,
            li: ({ children }) => <li>{badgify(children)}</li>,
          }}
        >
          {result.answer}
        </ReactMarkdown>
      </div>
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

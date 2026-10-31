import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { QueryResult } from '../api'
import { sourceLabel } from '../format'

// Answers are LLM output: react-markdown renders no raw HTML, so keep rehype-raw out.
export default function Answer({ result }: { result: QueryResult }) {
  return (
    <div className="answer">
      {!result.found && (
        <p className="notice">
          The indexed documents do not answer this question.
        </p>
      )}
      <div className="markdown">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.answer}</ReactMarkdown>
      </div>
      {result.sources.length > 0 && (
        <>
          <h3>Sources</h3>
          <ul className="sources">
            {result.sources.map((source) => (
              <li key={source.ref} title={source.citation}>
                [{source.ref}] {sourceLabel(source)}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

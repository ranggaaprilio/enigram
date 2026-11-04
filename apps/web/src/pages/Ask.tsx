import { useEffect, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Link } from 'react-router'
import { ask, messageOf, type Mode, type QueryResult } from '../api'
import Answer from '../components/Answer'
import { formatSecs } from '../format'

export default function Ask() {
  const [question, setQuestion] = useState('')
  // 'auto' lets the router pick (scope auto); 'code' asks the graph-first code agent (scope
  // code); mix/naive the document index (scope docs).
  const [mode, setMode] = useState<Mode | 'code' | 'auto'>('auto')
  const [pending, setPending] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [result, setResult] = useState<(QueryResult & { id: number }) | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Local-LLM answers take 30-60 s: show that the request is alive.
  useEffect(() => {
    if (!pending) return
    const started = Date.now()
    setElapsed(0)
    const timer = setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 1000)
    return () => clearInterval(timer)
  }, [pending])

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    if (!question.trim() || pending) return
    setPending(true)
    setError(null)
    setResult(null)
    try {
      const q = question.trim()
      setResult(
        await (mode === 'auto'
          ? ask(q, 'mix', 'auto')
          : mode === 'code'
            ? ask(q, 'mix', 'code')
            : ask(q, mode, 'docs')),
      )
    } catch (e) {
      setError(messageOf(e))
    } finally {
      setPending(false)
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void submit()
  }

  return (
    <section>
      <h1>Ask</h1>
      <form className="ask-form" onSubmit={submit}>
        <label htmlFor="question">Question</label>
        <textarea
          id="question"
          rows={3}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Which datasets does the paper use?"
        />
        <div className="ask-controls">
          <label htmlFor="mode">Mode</label>
          <select
            id="mode"
            value={mode}
            onChange={(e) => setMode(e.target.value as Mode | 'code' | 'auto')}
          >
            <option value="auto">Auto (router)</option>
            <option value="mix">GraphRAG (mix)</option>
            <option value="naive">Vector only (naive)</option>
            <option value="code">Code (agent)</option>
          </select>
          <button type="submit" disabled={pending || !question.trim()}>
            Ask
          </button>
          <span className="muted small">Ctrl/⌘ + Enter</span>
        </div>
      </form>

      {pending && <p className="muted">Thinking… {elapsed} s</p>}
      {error && <p className="error-box">{error}</p>}
      {result && (
        <>
          <Answer result={result} />
          <p className="muted small">
            {formatSecs(result.query_secs)} · {result.mode} ·{' '}
            <Link to={`/trace/${result.id}`}>View trace</Link>
          </p>
        </>
      )}
    </section>
  )
}

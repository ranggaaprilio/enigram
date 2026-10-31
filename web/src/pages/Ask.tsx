import { useEffect, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Link } from 'react-router'
import { ask, messageOf, type Mode, type QueryResult } from '../api'
import Answer from '../components/Answer'
import { formatSecs } from '../format'

export default function Ask() {
  const [question, setQuestion] = useState('')
  const [mode, setMode] = useState<Mode>('mix')
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
      setResult(await ask(question.trim(), mode))
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
          <select id="mode" value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
            <option value="mix">GraphRAG (mix)</option>
            <option value="naive">Vector only (naive)</option>
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

import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { WsLink } from '@/workspace'
import { ask, messageOf, type Mode, type QueryResult } from '../api'
import { Clock, Loader2, Sparkles } from 'lucide-react'
import ErrorAlert from '@/components/ErrorAlert'
import PageHeader from '@/components/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { Textarea } from '@/components/ui/textarea'
import Answer from '../components/Answer'
import { formatSecs, modeLabel } from '../format'

// Shown in the empty state; a click puts the question in the box.
const EXAMPLES = [
  'Which datasets does the paper use?',
  'What happens when a user logs out?',
  'Which documents mention the release gate?',
]

export default function Ask() {
  const [question, setQuestion] = useState('')
  // 'auto' lets the router pick (scope auto); 'code' asks the graph-first code agent (scope
  // code); mix/naive the document index (scope docs).
  const [mode, setMode] = useState<Mode | 'code' | 'auto'>('auto')
  const [pending, setPending] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [result, setResult] = useState<(QueryResult & { id: number }) | null>(null)
  const [error, setError] = useState<string | null>(null)
  const box = useRef<HTMLTextAreaElement>(null)

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
      <PageHeader
        title="Ask"
        description="Ask about the indexed documents and repositories. Every sentence of the answer says where it came from."
      />
      <form className="flex flex-col gap-3" onSubmit={submit}>
        <Label htmlFor="question" className="text-muted-foreground">
          Question
        </Label>
        <Textarea
          id="question"
          ref={box}
          rows={4}
          className="min-h-28"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Which datasets does the paper use?"
        />
        <div className="flex flex-wrap items-center gap-3">
          <Label htmlFor="mode" className="text-muted-foreground">
            Mode
          </Label>
          <NativeSelect
            id="mode"
            value={mode}
            onChange={(e) => setMode(e.target.value as Mode | 'code' | 'auto')}
          >
            <NativeSelectOption value="auto">Auto · pick for me</NativeSelectOption>
            <NativeSelectOption value="mix">Documents · graph + vectors</NativeSelectOption>
            <NativeSelectOption value="naive">Documents · vectors only</NativeSelectOption>
            <NativeSelectOption value="code">Code · repository agent</NativeSelectOption>
          </NativeSelect>
          <Button type="submit" disabled={pending || !question.trim()}>
            Ask
          </Button>
          <span className="text-[13px] text-muted-foreground max-sm:hidden">
            ⌘/Ctrl + Enter to send
          </span>
        </div>
      </form>

      {pending && (
        <p role="status" className="my-6 flex items-center gap-2 text-muted-foreground">
          <Loader2 className="size-4 animate-spin text-ring" /> Thinking… {elapsed} s
        </p>
      )}
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {result && (
        <>
          <Answer result={result} />
          <p className="mt-4 flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground">
            <Badge variant="outline" className="text-muted-foreground">
              <Clock /> {formatSecs(result.query_secs)}
            </Badge>
            <Badge variant="outline" className="text-muted-foreground">
              {modeLabel(result.mode)}
            </Badge>
            <WsLink to={`/trace/${result.id}`}>View trace</WsLink>
          </p>
        </>
      )}
      {!result && !pending && !error && (
        // DESIGN.md spotlight card: the one gradient on the page, gone once there is an answer.
        <div className="mt-8 rounded-2xl bg-(image:--spotlight) p-8 text-white">
          <h2 className="mt-0 mb-2 flex items-center gap-2 text-2xl font-medium tracking-[-0.8px]">
            <Sparkles className="size-5" /> What do you want to know?
          </h2>
          <p className="mb-5 max-w-[60ch] text-white/90">
            Answers come from the documents and code you indexed, with a source for every sentence.
            Try one of these:
          </p>
          <div className="flex flex-wrap gap-2">
            {EXAMPLES.map((example) => (
              <Button
                key={example}
                type="button"
                className="h-auto min-h-9 bg-white/15 py-2 text-left whitespace-normal text-white hover:bg-white/25"
                onClick={() => {
                  setQuestion(example)
                  box.current?.focus()
                }}
              >
                {example}
              </Button>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

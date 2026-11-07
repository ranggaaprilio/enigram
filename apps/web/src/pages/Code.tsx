import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router'
import { WsLink } from '@/workspace'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, ChevronRight, Plus, X } from 'lucide-react'
import {
  ApiError,
  getSymbol,
  getTree,
  listFeatures,
  listRepos,
  messageOf,
  NOTE_TYPES,
  patchAnnotation,
  reviewSymbol,
  unreviewSymbol,
  type Feature,
  type FeatureList,
  type RepoRow,
  type SymbolView,
  type TreeFile,
} from '../api'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import GuidedNoteForm, { type GuidedMode } from '@/components/GuidedNoteForm'
import PageHeader from '@/components/PageHeader'
import RecheckNote from '@/components/RecheckNote'
import StatusBadge from '@/components/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { cn } from '@/lib/utils'
import { formatTime } from '../format'

const muted = 'text-[13px] text-muted-foreground'
// The left list scrolls inside its card, under its filter and count; on wide screens the
// column sticks below the header like the notes panel (toggle + gap = the extra 3.5rem).
// The filter and count keep their height; only the list (listScroll, flex-1) gives way.
const listCard =
  'flex max-h-[60vh] flex-col rounded-xl bg-card p-3 lg:max-h-[calc(100vh-9.5rem)] [&>input]:shrink-0 [&>p]:shrink-0'
const listScroll = '-mx-1 min-h-0 flex-1 overflow-y-auto px-1'
const heading = 'mt-4 mb-1 text-sm font-semibold'
const NAME_KEY = 'enigram.by'

// The reviewer's name, kept in this browser only; storage can throw in private windows.
function readName(): string {
  try {
    return localStorage.getItem(NAME_KEY) ?? ''
  } catch {
    return ''
  }
}

// A repository browsed by its entry points (features) or, for developers, by its files and their
// symbols: the selected symbol's summary, steps, callers and code in the middle, with "looks
// right" / "not right" / "add context" under the summary; its notes and the guided note form on
// the right. Repository, mode, symbol and filter live in the URL, so a view can be linked.
export default function Code() {
  const [params, setParams] = useSearchParams()
  const [repos, setRepos] = useState<RepoRow[] | null>(null)
  const [features, setFeatures] = useState<FeatureList | null>(null)
  const [tree, setTree] = useState<TreeFile[] | null>(null)
  const [view, setView] = useState<SymbolView | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<GuidedMode | null>(null)
  const [by, setBy] = useState(readName)
  // An unknown ?repo= (deleted repository, old link) falls back to the first one.
  const asked = Number(params.get('repo'))
  const repoId = repos?.some((r) => r.id === asked) ? asked : (repos?.[0]?.id ?? null)
  const symbolId = params.get('symbol')
  const q = params.get('q') ?? ''
  const files = params.get('view') === 'files'
  // What is on screen now, so a reload that finishes after the user moved on is dropped.
  const shown = useRef('')
  shown.current = `${repoId}\u0000${symbolId}\u0000${q}\u0000${files}`

  function setParam(key: string, value: string, replace = false) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    if (key === 'repo') next.delete('symbol')
    // The two filters match different things: a new repository or mode starts unfiltered.
    if (key === 'repo' || key === 'view') next.delete('q')
    setParams(next, { replace })
  }

  useEffect(() => {
    listRepos()
      .then(setRepos)
      .catch((e) => setError(messageOf(e)))
  }, [])

  // A new repository clears the lists; a new filter keeps them until the new one arrives.
  useEffect(() => {
    setFeatures(null)
    setTree(null)
  }, [repoId])

  useEffect(() => {
    setError(null)
    if (!repoId || files) return
    // A slower response for an older filter must not replace this one.
    let current = true
    listFeatures(repoId, q)
      .then((f) => current && setFeatures(f))
      .catch((e) => current && setError(messageOf(e)))
    return () => {
      current = false
    }
  }, [repoId, q, files])

  // The file list is filtered in the browser: one request per repository.
  useEffect(() => {
    if (!repoId || !files) return
    let current = true
    getTree(repoId)
      .then((t) => current && setTree(t.files))
      .catch((e) => current && setError(messageOf(e)))
    return () => {
      current = false
    }
  }, [repoId, files])

  useEffect(() => {
    setView(null)
    setNotFound(false)
    setError(null)
    setMode(null)
    if (!repoId || !symbolId) return
    let current = true
    getSymbol(repoId, symbolId)
      .then((v) => current && setView(v))
      .catch((e) => {
        if (!current) return
        if (e instanceof ApiError && e.status === 404) setNotFound(true)
        else setError(messageOf(e))
      })
    return () => {
      current = false
    }
  }, [repoId, symbolId])

  // After a note is saved: its list on the right and the counts on the left.
  const reload = useCallback(async () => {
    if (!repoId || !symbolId) return
    const asked = shown.current
    if (files) {
      const [v, t] = await Promise.all([getSymbol(repoId, symbolId), getTree(repoId)])
      if (shown.current !== asked) return
      setView(v)
      setTree(t.files)
      return
    }
    const [v, f] = await Promise.all([getSymbol(repoId, symbolId), listFeatures(repoId, q)])
    if (shown.current !== asked) return
    setView(v)
    setFeatures(f)
  }, [repoId, symbolId, q, files])

  function saveName(value: string) {
    setBy(value)
    try {
      localStorage.setItem(NAME_KEY, value)
    } catch {
      // not kept: private window or blocked storage
    }
  }

  async function act(call: () => Promise<unknown>) {
    try {
      await call()
      setError(null)
      await reload()
    } catch (e) {
      setError(messageOf(e))
    }
  }

  const header = (
    <PageHeader
      title="Code"
      description="Browse an indexed repository by its entry points or its files, read what each one does, and leave a note on it."
    />
  )

  if (repos && repos.length === 0) {
    return (
      <section>
        {header}
        <Empty>
          No repository indexed yet. Index one on the{' '}
          <WsLink to="/documents">Documents</WsLink> page.
        </Empty>
      </section>
    )
  }

  return (
    <section>
      {header}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Label htmlFor="code-repo" className="text-muted-foreground">
          Repository
        </Label>
        <NativeSelect
          id="code-repo"
          value={repoId ?? ''}
          onChange={(e) => setParam('repo', e.target.value)}
        >
          {(repos ?? []).map((r) => (
            <NativeSelectOption key={r.id} value={r.id}>
              {r.name}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <Label htmlFor="code-by" className="text-muted-foreground">
          Your name
        </Label>
        <Input
          id="code-by"
          className="w-44"
          value={by}
          placeholder="shown on your reviews"
          onChange={(e) => saveName(e.target.value)}
        />
      </div>
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <div className="grid items-start gap-4 lg:grid-cols-[280px_minmax(0,1fr)_320px]">
        <div className="flex min-w-0 flex-col gap-2 lg:sticky lg:top-20">
          <div role="group" aria-label="Browse by" className="flex gap-1 rounded-xl bg-card p-1">
            {(
              [
                ['Features', false],
                ['Files', true],
              ] as const
            ).map(([label, value]) => (
              <Button
                key={label}
                type="button"
                size="sm"
                variant={files === value ? 'secondary' : 'ghost'}
                className="flex-1"
                aria-pressed={files === value}
                onClick={() => files !== value && setParam('view', value ? 'files' : '')}
              >
                {label}
              </Button>
            ))}
          </div>
          {files ? (
            <Files
              tree={tree}
              q={q}
              selected={symbolId}
              onFilter={(value) => setParam('q', value, true)}
              onPick={(id) => setParam('symbol', id)}
            />
          ) : (
            <Features
              list={features}
              q={q}
              selected={symbolId}
              onFilter={(value) => setParam('q', value, true)}
              onPick={(id) => setParam('symbol', id)}
            />
          )}
        </div>
        {!symbolId ? (
          <Empty>Pick a {files ? 'symbol' : 'feature'} to see what it does.</Empty>
        ) : notFound ? (
          <Empty>Symbol not found. It may have been removed by a re-index.</Empty>
        ) : !view ? (
          <p className="text-muted-foreground">Loading…</p>
        ) : (
          <>
            <SymbolPanel
              view={view}
              onPick={(id) => setParam('symbol', id)}
              onMode={setMode}
              onReview={() => repoId && act(() => reviewSymbol(repoId, view.symbol.id, by))}
              onUndo={() => repoId && act(() => unreviewSymbol(repoId, view.symbol.id))}
            />
            {/* Sticky on wide screens: the form opened from under a long summary stays in view. */}
            <div className="rounded-xl bg-card px-5 py-4 lg:sticky lg:top-20 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto">
              <h3 className="mb-2 text-sm font-semibold">Notes ({view.notes.length})</h3>
              {view.notes.length === 0 ? (
                <p className={muted}>No notes on this symbol yet.</p>
              ) : (
                <ul className="mb-2 flex flex-col gap-3">
                  {view.notes.map((n) => (
                    <li key={n.id} className="text-sm">
                      <Badge variant="secondary" className="mr-1.5 rounded-[4px]">
                        {NOTE_TYPES[n.note_type]}
                      </Badge>
                      {n.text}
                      <div className={cn('mt-1 flex flex-wrap items-center gap-1.5', muted)}>
                        <StatusBadge status={n.status} />
                        {n.conflict && (
                          <Badge variant="outline" className="border-destructive text-destructive">
                            Conflicts with code
                          </Badge>
                        )}
                        {n.by && <span>by {n.by}</span>}
                        <span>{formatTime(n.created_at)}</span>
                      </div>
                      {n.status === 'needs_verification' && (
                        <RecheckNote
                          note={n}
                          summary={view.summary}
                          onAct={(body) => act(() => patchAnnotation(n.id, body))}
                        />
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {mode && repoId ? (
                <GuidedNoteForm
                  key={view.symbol.id}
                  repoId={repoId}
                  symbolId={view.symbol.id}
                  mode={mode}
                  by={by}
                  onSaved={async () => {
                    setMode(null)
                    setError(null)
                    await reload()
                  }}
                  onCancel={() => setMode(null)}
                  onError={setError}
                />
              ) : (
                <p className={cn('mt-3', muted)}>
                  To add a note, use Not right or Add context under the summary.
                </p>
              )}
            </div>
          </>
        )}
      </div>
    </section>
  )
}

// Feature list groups, in display order; a kind not listed falls under "Other".
const KIND_LABELS: [string, string][] = [
  ['http_route', 'Routes'],
  ['page', 'Pages'],
  ['cron', 'Cron'],
  ['job', 'Jobs'],
  ['event', 'Events'],
  ['middleware', 'Middleware'],
  ['action', 'Server actions'],
  ['cli_main', 'Main'],
  ['root', 'Functions'],
]

function byKind(features: Feature[]): [string, Feature[]][] {
  const known = new Map(KIND_LABELS)
  const groups = new Map<string, Feature[]>(
    [...KIND_LABELS.map(([, label]) => label), 'Other'].map((label) => [label, []]),
  )
  for (const f of features) groups.get(known.get(f.kind) ?? 'Other')!.push(f)
  return [...groups].filter(([, rows]) => rows.length > 0)
}

function Features(props: {
  list: FeatureList | null
  q: string
  selected: string | null
  onFilter: (value: string) => void
  onPick: (symbolId: string) => void
}) {
  const { list } = props
  return (
    <div className={listCard}>
      <Input
        aria-label="Filter features"
        placeholder="Filter by route or name"
        value={props.q}
        onChange={(e) => props.onFilter(e.target.value)}
      />
      {list === null ? (
        <p className={cn('mt-2 px-2', muted)}>Loading…</p>
      ) : (
        <>
          <p className={cn('mt-2 mb-1 px-2', muted)}>
            {list.features.length < list.total
              ? `${list.features.length} of ${list.total} features — narrow with the filter`
              : `${list.total} ${list.total === 1 ? 'feature' : 'features'}`}
            {` · ${list.features.filter((f) => f.reviewed).length} reviewed`}
            {list.features.length < list.total && ' in this list'}
            {list.features.some((f) => f.kind === 'root') &&
              ' · no framework adapter: showing top-level functions'}
          </p>
          <div className={listScroll}>
            {list.features.length === 0 ? (
              <p className={cn('px-2 py-4', muted)}>
                {props.q ? 'No features match.' : 'No entry points found in this repository.'}
              </p>
            ) : (
              byKind(list.features).map(([label, features], _, groups) => (
                <div key={label}>
                  {groups.length > 1 && (
                    <h4 className={cn('mt-3 mb-1 px-2 font-semibold uppercase', muted)}>
                      {label}
                    </h4>
                  )}
                  <ul>
                    {features.map((f) => (
                      <li key={`${f.symbol_id}\u0000${f.route}`}>
                        <button
                          type="button"
                          className={cn(
                            'flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-secondary',
                            f.symbol_id === props.selected && 'bg-secondary',
                          )}
                          onClick={() => props.onPick(f.symbol_id)}
                        >
                          {f.reviewed ? (
                            <Check className="size-3.5 shrink-0 text-success" aria-label="reviewed" />
                          ) : (
                            <span className="size-3.5 shrink-0" />
                          )}
                          <span
                            className="min-w-0 flex-1 truncate font-mono text-[13px]"
                            title={`${f.qualified_name} · ${f.file}`}
                          >
                            {f.route}
                          </span>
                          <Badge variant="outline" className="rounded-[4px] text-muted-foreground">
                            {f.notes}
                          </Badge>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))
            )}
          </div>
        </>
      )}
    </div>
  )
}

const ROOT_DIR = '(root)'

function dirOf(path: string): string {
  const slash = path.lastIndexOf('/')
  return slash < 0 ? ROOT_DIR : path.slice(0, slash)
}

// Files under their folder, root files first; folders and files in path order.
function byDir(files: TreeFile[]): [string, TreeFile[]][] {
  const groups = new Map<string, TreeFile[]>()
  for (const f of [...files].sort((a, b) => a.path.localeCompare(b.path))) {
    const dir = dirOf(f.path)
    groups.set(dir, [...(groups.get(dir) ?? []), f])
  }
  return [...groups].sort(([a], [b]) =>
    a === ROOT_DIR ? -1 : b === ROOT_DIR ? 1 : a.localeCompare(b),
  )
}

// Every indexed file by folder; a file opens to its symbol outline (members under their class).
// The filter matches a path or a symbol name; files where a symbol matched open by themselves,
// as does the file of the open symbol (listed whatever the filter), until the user closes them.
function Files(props: {
  tree: TreeFile[] | null
  q: string
  selected: string | null
  onFilter: (value: string) => void
  onPick: (symbolId: string) => void
}) {
  const { tree } = props
  const [toggled, setToggled] = useState<Map<string, boolean>>(new Map())
  const needle = props.q.trim().toLowerCase()
  const selectedFile = props.selected?.split('#')[0] ?? null
  const symbolMatch = (f: TreeFile) =>
    needle !== '' && f.symbols.some((s) => s.qualified_name.toLowerCase().includes(needle))
  // The open symbol's file stays listed: a caller opened from another file must stay visible.
  const shown = (tree ?? []).filter(
    (f) =>
      !needle || f.path === selectedFile || f.path.toLowerCase().includes(needle) || symbolMatch(f),
  )
  const symbolCount = (tree ?? []).reduce((n, f) => n + f.symbols.length, 0)

  function toggle(path: string, open: boolean) {
    setToggled((prev) => new Map(prev).set(path, !open))
  }

  return (
    <div className={listCard}>
      <Input
        aria-label="Filter files"
        placeholder="Filter by path or symbol"
        value={props.q}
        onChange={(e) => props.onFilter(e.target.value)}
      />
      {tree === null ? (
        <p className={cn('mt-2 px-2', muted)}>Loading…</p>
      ) : (
        <>
          <p className={cn('mt-2 mb-1 px-2', muted)}>
            {needle
              ? `${shown.length} of ${tree.length} files`
              : `${tree.length} ${tree.length === 1 ? 'file' : 'files'} · ${symbolCount} ${symbolCount === 1 ? 'symbol' : 'symbols'}`}
          </p>
          <div className={listScroll}>
            {shown.length === 0 ? (
              <p className={cn('px-2 py-4', muted)}>
                {needle ? 'No files match.' : 'No files indexed in this repository.'}
              </p>
            ) : (
              byDir(shown).map(([dir, rows]) => (
                <div key={dir}>
                  <h4
                    className={cn('mt-3 mb-1 px-2 font-mono [overflow-wrap:anywhere]', muted)}
                    title={dir}
                  >
                    {dir}
                  </h4>
                  <ul>
                    {rows.map((f) => {
                      const open =
                        toggled.get(f.path) ?? (f.path === selectedFile || symbolMatch(f))
                      const notes = f.symbols.reduce((n, s) => n + s.notes, 0)
                      return (
                        <li key={f.path}>
                          <button
                            type="button"
                            disabled={f.symbols.length === 0}
                            aria-expanded={f.symbols.length > 0 ? open : undefined}
                            title={f.path}
                            className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-secondary disabled:cursor-default disabled:opacity-60 disabled:hover:bg-transparent"
                            onClick={() => toggle(f.path, open)}
                          >
                            <ChevronRight
                              className={cn(
                                'size-3.5 shrink-0 text-muted-foreground transition-transform',
                                open && 'rotate-90',
                                f.symbols.length === 0 && 'invisible',
                              )}
                            />
                            <span className="min-w-0 flex-1 truncate font-mono text-[13px]">
                              {f.path.slice(f.path.lastIndexOf('/') + 1)}
                            </span>
                            {notes > 0 && (
                              <Badge
                                variant="outline"
                                className="rounded-[4px] text-muted-foreground"
                              >
                                {notes}
                              </Badge>
                            )}
                          </button>
                          {open && f.symbols.length > 0 && (
                            <ul className="mb-1">
                              {f.symbols.map((s) => (
                                <li key={s.id}>
                                  <button
                                    type="button"
                                    aria-current={s.id === props.selected ? 'true' : undefined}
                                    title={`${s.qualified_name} · lines ${s.line_start}-${s.line_end}`}
                                    className={cn(
                                      'flex w-full cursor-pointer items-center gap-2 rounded-md py-1 pr-2 text-left text-sm hover:bg-secondary',
                                      s.parent ? 'pl-12' : 'pl-7',
                                      s.id === props.selected && 'bg-secondary',
                                    )}
                                    onClick={() => props.onPick(s.id)}
                                  >
                                    <span className="shrink-0 text-xs text-muted-foreground">
                                      {s.kind}
                                    </span>{' '}
                                    <span className="min-w-0 flex-1 truncate font-mono text-[13px]">
                                      {s.parent
                                        ? s.qualified_name.slice(s.parent.length + 1)
                                        : s.qualified_name}
                                    </span>{' '}
                                    <Badge
                                      variant="outline"
                                      className="rounded-[4px] text-muted-foreground"
                                    >
                                      {s.notes}
                                    </Badge>
                                  </button>
                                </li>
                              ))}
                            </ul>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                </div>
              ))
            )}
          </div>
        </>
      )}
    </div>
  )
}

function SymbolPanel({
  view,
  onPick,
  onMode,
  onReview,
  onUndo,
}: {
  view: SymbolView
  onPick: (symbolId: string) => void
  onMode: (mode: GuidedMode) => void
  onReview: () => void
  onUndo: () => void
}) {
  const summary = view.summary.flow ?? view.summary.symbol
  const review = view.review
  const body = view.symbol.body
  return (
    <div className="min-w-0">
      <div className="rounded-xl bg-card px-5 py-4">
        <h2 className="mt-0 mb-2 [overflow-wrap:anywhere]">{view.symbol.qualified_name}</h2>
        <div className="flex flex-wrap items-center gap-1.5">
          {view.entrypoints.map((e) => (
            <Badge key={`${e.kind}\u0000${e.route}`} variant="outline">
              {e.kind} · {e.route}
            </Badge>
          ))}
          <span className={cn('[overflow-wrap:anywhere]', muted)}>{view.location}</span>
        </div>
        <h3 className={heading}>Summary</h3>
        {summary ? (
          <div className="markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{summary}</ReactMarkdown>
          </div>
        ) : (
          <p className={muted}>No summary yet.</p>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          {summary && !review?.current && (
            <Button type="button" size="sm" variant="secondary" onClick={onReview}>
              <Check /> Looks right
            </Button>
          )}
          {summary && (
            <Button
              type="button"
              size="sm"
              variant="soft-destructive"
              onClick={() => onMode('mismatch')}
            >
              <X /> Not right
            </Button>
          )}
          <Button type="button" size="sm" variant="secondary" onClick={() => onMode('context')}>
            <Plus /> Add context
          </Button>
        </div>
        {review && (
          <p className={cn('mt-2', muted)}>
            Reviewed by {review.by || review.actor} · {formatTime(review.at)}
            {!review.current && ' — the summary changed since'}
            {' · '}
            <Button
              type="button"
              variant="link"
              className="h-auto p-0 text-[13px] text-link"
              onClick={onUndo}
            >
              {review.current ? 'Undo' : 'Remove'}
            </Button>
          </p>
        )}
        <h3 className={heading}>Steps ({view.steps.length})</h3>
        {view.steps.length === 0 ? (
          <p className={muted}>No steps: this symbol calls nothing else in the repository.</p>
        ) : (
          <ol className="flex flex-col gap-2">
            {view.steps.map((s) => (
              <li key={s.id}>
                <Button
                  type="button"
                  variant="link"
                  className="h-auto p-0 text-link"
                  onClick={() => onPick(s.id)}
                >
                  {s.qualified_name}
                </Button>{' '}
                <Badge variant="outline" className="rounded-[4px] text-muted-foreground">
                  {s.notes} {s.notes === 1 ? 'note' : 'notes'}
                </Badge>
                {s.summary ? (
                  <div className={cn('markdown', muted)}>
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{s.summary}</ReactMarkdown>
                  </div>
                ) : (
                  <div className={muted}>No summary.</div>
                )}
              </li>
            ))}
          </ol>
        )}
        <h3 className={heading}>Called by ({view.callers.length})</h3>
        {view.callers.length === 0 ? (
          <p className={muted}>
            Nothing in the repository calls it
            {view.entrypoints.length > 0 ? ': it is an entry point.' : '.'}
          </p>
        ) : (
          <ul className="flex flex-col gap-1">
            {view.callers.map((c) => (
              <li key={c.id}>
                <Button
                  type="button"
                  variant="link"
                  className="h-auto p-0 text-link"
                  onClick={() => onPick(c.id)}
                >
                  {c.qualified_name}
                </Button>{' '}
                <Badge variant="outline" className="rounded-[4px] text-muted-foreground">
                  {c.notes} {c.notes === 1 ? 'note' : 'notes'}
                </Badge>{' '}
                <span className={cn('[overflow-wrap:anywhere]', muted)}>
                  {c.file}:{c.line_start}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <Section title="Code" count={body.split('\n').length}>
        <pre className="rounded-md bg-background px-4 py-3 text-[13px] leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
          {body}
        </pre>
      </Section>
    </div>
  )
}

// As Trace's sections, but closed by default: PM/QA read the summary, not the code.
function Section({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <Collapsible className="group my-3 rounded-xl bg-card">
      <CollapsibleTrigger className="flex w-full cursor-pointer items-center gap-2 px-5 py-4 text-left font-semibold tracking-[-0.3px]">
        <ChevronRight className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        {title} ({count} lines)
      </CollapsibleTrigger>
      <CollapsibleContent forceMount className="px-2 pb-3 data-[state=closed]:hidden">
        {children}
      </CollapsibleContent>
    </Collapsible>
  )
}

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { patchMessage } from '../toasts'
import { toast } from 'sonner'
import { useSearchParams } from 'react-router'
import { WsLink } from '@/workspace'
import { useAccess } from '../access'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, ChevronRight, Plus, X } from 'lucide-react'
import {
  ApiError,
  deleteAnnotation,
  getSymbol,
  getTree,
  listFeatures,
  listRepos,
  messageOf,
  patchAnnotation,
  reviewSymbol,
  unreviewSymbol,
  type AnnotationPatch,
  type Feature,
  type FeatureList,
  type RepoRow,
  type SymbolView,
  type TreeFile,
} from '../api'
import Empty from '@/components/Empty'
import Loading from '@/components/Loading'
import ErrorAlert from '@/components/ErrorAlert'
import AddNoteForm, { type NoteMode } from '@/components/AddNoteForm'
import NoteCard from '@/components/NoteCard'
import PageHeader from '@/components/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { cn } from '@/lib/utils'
import { formatTime } from '../format'

const muted = 'text-[13px] text-muted-foreground'
// The left list scrolls inside its card, under its filter and count; on wide screens the
// column sticks below the header (toggle + gap = the extra 3.5rem).
// The filter and count keep their height; only the list (listScroll, flex-1) gives way.
const listCard =
  'flex max-h-[60vh] flex-col rounded-md bg-card p-3 lg:max-h-[calc(100vh-9.8rem)] [&>input]:shrink-0 [&>p]:shrink-0'
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
// symbols: the selected symbol on the right, in tabs. Overview has its summary, steps and callers,
// with "looks right" / "not right" / "add context" under the summary; Notes its notes and the
// guided note form; Code its source. Repository, mode, symbol, filter, tab and a linked note live
// in the URL, so a view can be linked.
// `embedded`: inside the Knowledge page, which has the header (plan item 5, A2).
export default function Code({ embedded = false }: { embedded?: boolean }) {
  const [params, setParams] = useSearchParams()
  const [repos, setRepos] = useState<RepoRow[] | null>(null)
  const [features, setFeatures] = useState<FeatureList | null>(null)
  const [tree, setTree] = useState<TreeFile[] | null>(null)
  const [view, setView] = useState<SymbolView | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<NoteMode | null>(null)
  const [by, setBy] = useState(readName)
  const access = useAccess()
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
    // A linked note belongs to the symbol it was linked on.
    if (key === 'repo' || key === 'symbol') next.delete('note')
    setParams(next, { replace })
  }

  // The symbol's tab: Overview by default, Notes when a note is linked (?note=), else ?panel=.
  const noteId = Number(params.get('note')) || null
  const asked_panel = params.get('panel')
  const panel: Panel = noteId
    ? 'notes'
    : asked_panel === 'notes' || asked_panel === 'code'
      ? asked_panel
      : 'overview'

  function setPanel(value: Panel) {
    const next = new URLSearchParams(params)
    if (value === 'overview') next.delete('panel')
    else next.set('panel', value)
    next.delete('note')
    setParams(next, { replace: true })
  }

  // "Not right" / "Add context" under the summary open the form on the Notes tab.
  function openForm(value: NoteMode) {
    setMode(value)
    if (panel !== 'notes') setPanel('notes')
  }

  function copyLink(id: number) {
    const next = new URLSearchParams(params)
    next.set('note', String(id))
    next.delete('panel')
    if (repoId) next.set('repo', String(repoId))
    const url = `${window.location.origin}${window.location.pathname}?${next}`
    // The clipboard needs a secure context (https or localhost).
    if (!navigator.clipboard) return void toast.error('Could not copy the link')
    navigator.clipboard.writeText(url).then(
      () => toast.success('Link copied'),
      () => toast.error('Could not copy the link'),
    )
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

  // Resolves true once the call went through, so a note card can close its editor or dialog; a
  // failed reload afterwards is shown but does not undo that.
  async function act(call: () => Promise<unknown>, done: string): Promise<boolean> {
    try {
      await call()
    } catch (e) {
      setError(messageOf(e))
      return false
    }
    setError(null)
    toast.success(done)
    await reload().catch((e) => setError(messageOf(e)))
    return true
  }

  const header = embedded ? null : (
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
          No repository indexed yet. Index one on the <WsLink to="/pipeline">Pipeline</WsLink> page.
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
        {/* Signed in, the API puts the login's name on reviews and notes. */}
        {!access.user && (
          <>
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
          </>
        )}
      </div>
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <div className="grid items-start gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-2 lg:sticky lg:top-20">
          <div role="group" aria-label="Browse by" className="flex gap-1 rounded-md bg-card p-1">
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
          <Loading rows={6} />
        ) : (
          <SymbolPanel
            view={view}
            panel={panel}
            onPanel={setPanel}
            onPick={(id) => setParam('symbol', id)}
            onMode={openForm}
            onReview={() =>
              repoId && act(() => reviewSymbol(repoId, view.symbol.id, by), 'Marked as right')
            }
            onUndo={() =>
              repoId && act(() => unreviewSymbol(repoId, view.symbol.id), 'Review removed')
            }
            notes={
              <NotesTab
                view={view}
                noteId={noteId}
                onAdd={() => setMode('context')}
                form={
                  mode &&
                  repoId && (
                    <AddNoteForm
                      key={view.symbol.id}
                      title={mode === 'mismatch' ? 'Not right' : 'Add context'}
                      target={{
                        kind: 'symbol',
                        target_kind: 'symbol',
                        target_ref: view.symbol.id,
                        repo: repoId,
                        repo_name: null,
                        label: view.symbol.qualified_name,
                        detail: view.location,
                        summary: null, // the summary is one tab away, in the header's symbol
                      }}
                      fixedTarget
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
                  )
                }
                onPatch={(id, body) => act(() => patchAnnotation(id, body), patchMessage(body))}
                onDelete={(id) => act(() => deleteAnnotation(id), 'Note deleted')}
                onCopyLink={copyLink}
              />
            }
          />
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

// A linked or newly opened symbol may sit below the fold of a long list: centre its row
// (aria-current="true") in the list. Only the list scrolls; scrollIntoView would move the page
// as well. A row already in view leaves the list where it is.
function useScrollToCurrent(selected: string | null, items: unknown) {
  const scroller = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const list = scroller.current
    const open = list?.querySelector<HTMLElement>('[aria-current="true"]')
    if (!list || !open) return
    const box = list.getBoundingClientRect()
    const row = open.getBoundingClientRect()
    if (row.top >= box.top && row.bottom <= box.bottom) return
    list.scrollTop += row.top - box.top - (box.height - row.height) / 2
  }, [selected, items])
  return scroller
}

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
  const scroller = useScrollToCurrent(props.selected, list)
  return (
    <div className={listCard}>
      <Input
        aria-label="Filter features"
        placeholder="Filter by route or name"
        value={props.q}
        onChange={(e) => props.onFilter(e.target.value)}
      />
      {list === null ? (
        <Loading rows={8} />
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
          <div ref={scroller} className={listScroll}>
            {list.features.length === 0 ? (
              <p className={cn('px-2 py-4', muted)}>
                {props.q ? 'No features match.' : 'No entry points found in this repository.'}
              </p>
            ) : (
              byKind(list.features).map(([label, features], _, groups) => (
                <div key={label}>
                  {groups.length > 1 && (
                    <h2 className={cn('mt-3 mb-1 px-2 font-semibold uppercase', muted)}>{label}</h2>
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
                          aria-current={f.symbol_id === props.selected ? 'true' : undefined}
                          onClick={() => props.onPick(f.symbol_id)}
                        >
                          {f.reviewed ? (
                            <Check
                              className="size-3.5 shrink-0 text-success"
                              aria-label="reviewed"
                            />
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

  const scroller = useScrollToCurrent(props.selected, tree)

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
        <Loading rows={8} />
      ) : (
        <>
          <p className={cn('mt-2 mb-1 px-2', muted)}>
            {needle
              ? `${shown.length} of ${tree.length} files`
              : `${tree.length} ${tree.length === 1 ? 'file' : 'files'} · ${symbolCount} ${symbolCount === 1 ? 'symbol' : 'symbols'}`}
          </p>
          <div ref={scroller} className={listScroll}>
            {shown.length === 0 ? (
              <p className={cn('px-2 py-4', muted)}>
                {needle ? 'No files match.' : 'No files indexed in this repository.'}
              </p>
            ) : (
              byDir(shown).map(([dir, rows]) => (
                <div key={dir}>
                  <h2
                    className={cn('mt-3 mb-1 px-2 font-mono [overflow-wrap:anywhere]', muted)}
                    title={dir}
                  >
                    {dir}
                  </h2>
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

type Panel = 'overview' | 'notes' | 'code'

function SymbolPanel({
  view,
  panel,
  onPanel,
  onPick,
  onMode,
  onReview,
  onUndo,
  notes,
}: {
  view: SymbolView
  panel: Panel
  onPanel: (panel: Panel) => void
  onPick: (symbolId: string) => void
  onMode: (mode: NoteMode) => void
  onReview: () => void
  onUndo: () => void
  notes: ReactNode // the Notes tab
}) {
  const summary = view.summary.flow ?? view.summary.symbol
  const review = view.review
  const body = view.symbol.body
  const flagged = view.notes.filter((n) => n.status === 'needs_verification').length
  // plans/rbac.md: contributors review and write notes; a ✓ is undone by whoever set it or a
  // maintainer.
  const access = useAccess()
  const contributor = access.can('contributor')
  const mayUndo =
    access.can('maintainer') || (contributor && review !== null && review.actor === access.name)
  return (
    <div className="min-w-0 rounded-md bg-card">
      <div className="px-5 pt-4">
        <h2 className="mt-0 mb-2 [overflow-wrap:anywhere]">{view.symbol.qualified_name}</h2>
        <div className="flex flex-wrap items-center gap-1.5">
          {view.entrypoints.map((e) => (
            <Badge key={`${e.kind}\u0000${e.route}`} variant="outline">
              {e.kind} · {e.route}
            </Badge>
          ))}
          <span className={cn('[overflow-wrap:anywhere]', muted)}>{view.location}</span>
        </div>
      </div>
      <Tabs value={panel} onValueChange={(value) => onPanel(value as Panel)} className="mt-3">
        <TabsList aria-label="Symbol" className="px-3">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="notes">
            <span>Notes ({view.notes.length})</span>
            {flagged > 0 && (
              <span
                className="rounded-[4px] bg-info-bg px-1.5 text-xs text-info"
                title={`${flagged} to re-check`}
              >
                {flagged} to re-check
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="code">Code ({body.split('\n').length} lines)</TabsTrigger>
        </TabsList>
        {/* Overview and Notes stay mounted: a draft note survives a look at the summary. */}
        <TabsContent
          value="overview"
          forceMount
          className="px-5 pt-1 pb-5 data-[state=inactive]:hidden"
        >
          <h3 className={heading}>Summary</h3>
          {summary ? (
            <div className="markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{summary}</ReactMarkdown>
            </div>
          ) : (
            <p className={muted}>No summary yet.</p>
          )}
          {contributor && (
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
          )}
          {review && (
            <p className={cn('mt-2', muted)}>
              Reviewed by {review.by || review.actor} · {formatTime(review.at)}
              {!review.current && ' — the summary changed since'}
              {mayUndo && (
                <>
                  {' · '}
                  <Button
                    type="button"
                    variant="link"
                    className="h-auto p-0 text-[13px] text-link"
                    onClick={onUndo}
                  >
                    {review.current ? 'Undo' : 'Remove'}
                  </Button>
                </>
              )}
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
        </TabsContent>
        <TabsContent
          value="notes"
          forceMount
          className="px-5 pt-4 pb-5 data-[state=inactive]:hidden"
        >
          {notes}
        </TabsContent>
        {/* PM/QA read the summary, not the code: its tab mounts only when opened. */}
        <TabsContent value="code" className="p-3">
          <pre className="rounded-md bg-background px-4 py-3 text-[13px] leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
            {body}
          </pre>
        </TabsContent>
      </Tabs>
    </div>
  )
}

// The symbol's notes as cards, newest form on top; a linked note (?note=) is outlined and
// scrolled to.
function NotesTab(props: {
  view: SymbolView
  noteId: number | null
  form: ReactNode // the open note form, if any
  onAdd: () => void
  onPatch: (id: number, body: AnnotationPatch) => Promise<boolean>
  onDelete: (id: number) => Promise<boolean>
  onCopyLink: (id: number) => void
}) {
  const { notes, summary } = props.view
  const contributor = useAccess().can('contributor')
  return (
    <div className="flex flex-col gap-3">
      <h3 className="sr-only">Notes</h3>
      {notes.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className={cn('m-0', muted)}>
            What the team knows that the code does not say: rules, expected behaviour, known issues.
          </p>
          {contributor && !props.form && (
            <Button type="button" size="sm" variant="secondary" onClick={props.onAdd}>
              <Plus /> Add note
            </Button>
          )}
        </div>
      )}
      {props.form}
      {notes.length === 0 ? (
        !props.form && (
          <div className="flex flex-col items-start gap-3 rounded-md border border-dashed px-5 py-6">
            <div>
              <p className="m-0 font-medium">No notes on this symbol yet.</p>
              <p className={cn('m-0 mt-1', muted)}>
                Add a business rule, what should happen, a known issue or a tip for developers.
                Answers cite verified notes.
              </p>
            </div>
            {contributor && (
              <Button type="button" size="sm" onClick={props.onAdd}>
                <Plus /> Add note
              </Button>
            )}
          </div>
        )
      ) : (
        <ul className="m-0 flex list-none flex-col gap-3 p-0">
          {notes.map((n) => (
            <li key={n.id}>
              <NoteCard
                note={n}
                summary={summary}
                highlighted={n.id === props.noteId}
                onPatch={(body) => props.onPatch(n.id, body)}
                onDelete={() => props.onDelete(n.id)}
                onCopyLink={() => props.onCopyLink(n.id)}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

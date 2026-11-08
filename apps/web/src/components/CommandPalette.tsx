import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { Code2, FileText, Layers, Network, Search } from 'lucide-react'
import { useAccess } from '../access'
import { messageOf, searchTargets, type Target, type Workspace } from '../api'
import { NAV } from '@/components/AppSidebar'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from '@/components/ui/command'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'

const SEARCH_DELAY_MS = 200
const MIN_QUERY = 2

const KIND_ICON = { feature: Code2, symbol: Code2, entity: Network, document: FileText }
const KIND_LABEL = {
  feature: 'Features',
  symbol: 'Code',
  entity: 'Entities',
  document: 'Documents',
}

// Where a search result opens: code on Knowledge, an entity on the Graph page.
function pathOf(t: Target): string {
  if (t.target_kind === 'symbol' && t.repo !== null)
    return `/knowledge?repo=${t.repo}&symbol=${encodeURIComponent(t.target_ref)}`
  if (t.target_kind === 'entity') return `/graph?q=${encodeURIComponent(t.target_ref)}`
  return '/documents'
}

// Plan item 4, U3: ⌘K / Ctrl+K moves anywhere without the mouse: a page, another workspace, or
// a feature, symbol, entity or document found by what you know (GET /targets).
export default function CommandPalette(props: { workspace: string; workspaces: Workspace[] }) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const access = useAccess()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [found, setFound] = useState<Target[]>([])
  const [error, setError] = useState<string | null>(null)
  // Opened by a shortcut, not a DialogTrigger: give focus back to what had it (U4).
  const opener = useRef<HTMLElement | null>(null)

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key.toLowerCase() === 'k' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault()
        setOpen((now) => !now)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    const q = query.trim()
    setError(null)
    if (q.length < MIN_QUERY) {
      setFound([])
      return
    }
    let current = true
    const timer = setTimeout(() => {
      searchTargets(q)
        .then((rows) => current && setFound(rows))
        .catch((e) => current && setError(messageOf(e)))
    }, SEARCH_DELAY_MS)
    return () => {
      current = false
      clearTimeout(timer)
    }
  }, [query])

  function go(path: string) {
    setOpen(false)
    setQuery('')
    navigate(path)
  }

  const needle = query.trim().toLowerCase()
  const matches = (text: string) => !needle || text.toLowerCase().includes(needle)
  const pages = NAV.flatMap((g) => g.pages)
    .filter((p) => !p.minimum || access.can(p.minimum))
    .filter((p) => matches(p.label))
  // /w/<ws>/<page>/...: another workspace opens the same page.
  const page = pathname.split('/')[3] || 'dashboard'
  const others = props.workspaces.filter((w) => w.name !== props.workspace && matches(w.name))
  const groups = (['feature', 'symbol', 'entity', 'document'] as const)
    .map((kind) => ({ kind, rows: found.filter((t) => t.kind === kind) }))
    .filter((g) => g.rows.length > 0)

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="ml-auto gap-2 text-muted-foreground"
        aria-label="Search and go to (⌘K)"
        onClick={() => setOpen(true)}
      >
        <Search aria-hidden />
        <span className="hidden sm:inline">Search</span>
        <kbd className="pointer-events-none hidden rounded-sm border bg-muted px-1.5 font-sans text-[11px] sm:inline">
          ⌘K
        </kbd>
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          className="overflow-hidden p-0"
          showCloseButton={false}
          onOpenAutoFocus={() => {
            opener.current = document.activeElement as HTMLElement | null
          }}
          onCloseAutoFocus={(event) => {
            if (opener.current?.isConnected && opener.current !== document.body) {
              event.preventDefault()
              opener.current.focus()
            }
          }}
        >
          <DialogTitle className="sr-only">Go to</DialogTitle>
          <DialogDescription className="sr-only">
            A page, a workspace, or something in the code and graph
          </DialogDescription>
          {/* Filtered here: the API already matched the search results. */}
          <Command shouldFilter={false} label="Go to">
            <CommandInput
              value={query}
              onValueChange={setQuery}
              placeholder="A page, a workspace, a route, a name…"
            />
            <CommandList>
              <CommandEmpty>
                {error ??
                  (needle.length >= MIN_QUERY ? 'Nothing matches.' : 'Type to search the code.')}
              </CommandEmpty>
              {pages.length > 0 && (
                <CommandGroup heading="Pages">
                  {pages.map(({ to, label, icon: Icon }) => (
                    <CommandItem
                      key={to}
                      value={`page ${to}`}
                      onSelect={() => go(`/w/${props.workspace}${to}`)}
                    >
                      <Icon aria-hidden />
                      {label}
                    </CommandItem>
                  ))}
                </CommandGroup>
              )}
              {others.length > 0 && (
                <CommandGroup heading="Switch workspace">
                  {others.map((w) => (
                    <CommandItem
                      key={w.name}
                      value={`workspace ${w.name}`}
                      onSelect={() => go(`/w/${w.name}/${page}`)}
                    >
                      <Layers aria-hidden />
                      {w.name}
                      <CommandShortcut>{page}</CommandShortcut>
                    </CommandItem>
                  ))}
                </CommandGroup>
              )}
              {groups.map(({ kind, rows }) => {
                const Icon = KIND_ICON[kind]
                return (
                  <CommandGroup key={kind} heading={KIND_LABEL[kind]}>
                    {rows.map((t) => (
                      <CommandItem
                        key={`${kind}\u0000${t.repo}\u0000${t.target_ref}`}
                        value={`${kind} ${t.repo} ${t.target_ref}`}
                        onSelect={() => go(`/w/${props.workspace}${pathOf(t)}`)}
                      >
                        <Icon aria-hidden />
                        <span className="min-w-0 flex-1 truncate">
                          {t.label}
                          <span className="ml-2 text-muted-foreground">
                            {t.repo_name ?? t.detail}
                          </span>
                        </span>
                      </CommandItem>
                    ))}
                  </CommandGroup>
                )
              })}
            </CommandList>
          </Command>
        </DialogContent>
      </Dialog>
    </>
  )
}

import { useEffect, useState } from 'react'
import { Toaster } from './components/ui/sonner'
import { Link, Navigate, Route, Routes, useLocation, useParams } from 'react-router'
import { accessOf, AccessProvider } from './access'
import {
  ApiError,
  getHealth,
  getMe,
  listWorkspaces,
  setWorkspace,
  type Me,
  type Workspace,
} from './api'
import AppSidebar, { NAV } from './components/AppSidebar'
import CommandPalette from './components/CommandPalette'
import Empty from './components/Empty'
import SignIn from './components/SignIn'
import { Separator } from './components/ui/separator'
import { SidebarInset, SidebarProvider, SidebarTrigger } from './components/ui/sidebar'
import Agents from './pages/Agents'
import Chat from './pages/Chat'
import Dashboard from './pages/Dashboard'
import Documents from './pages/Documents'
import RepoDetail from './pages/RepoDetail'
import Graph from './pages/Graph'
import Knowledge from './pages/Knowledge'
import Memory from './pages/Memory'
import Members from './pages/Members'
import Review from './pages/Review'
import Setup from './pages/Setup'
import { TraceDetail, TraceList } from './pages/Trace'
import { WORKSPACE_NAME } from './workspace'

// An old link (/code?repo=1&symbol=…, /annotations?id=7) opens the same view on Knowledge.
function Moved({ to, tab }: { to: string; tab?: string }) {
  const { workspace = '' } = useParams()
  const { search } = useLocation()
  const params = new URLSearchParams(search)
  if (tab) params.set('tab', tab)
  const query = params.toString()
  return <Navigate replace to={`/w/${workspace}/${to}${query ? `?${query}` : ''}`} />
}

function RepoMoved() {
  return <Moved to={`documents/repos/${useParams().id}`} />
}

// The item a page shows, for the breadcrumb: a query, a symbol, a note.
function itemOf(segment: string, pathname: string, search: string): string | null {
  const params = new URLSearchParams(search)
  if (segment === 'chat') {
    const id = pathname.split('/')[4]
    return id ? `Chat ${id}` : null
  }
  if (segment === 'trace') {
    const id = pathname.split('/')[4]
    return id ? `Query ${id}` : null
  }
  if (segment === 'documents') {
    const [, , , , kind, id] = pathname.split('/') // /w/<ws>/documents/repos/<id>
    return kind === 'repos' && id ? `Repository ${id}` : null
  }
  if (segment === 'knowledge') {
    if (params.get('id')) return `Note ${params.get('id')}`
    return params.get('symbol')?.split('#').pop() ?? null
  }
  return null
}

const PAGE_LABELS = Object.fromEntries(
  NAV.flatMap((group) => group.pages.map((p) => [p.to.slice(1), p.label])),
)

export default function App() {
  // The process workspace: where "/" and old links go; undefined until /health answers.
  const [home, setHome] = useState<string | undefined>(undefined)

  useEffect(() => {
    getHealth()
      .then((health) => setHome(health.workspace))
      .catch(() => setHome('default'))
  }, [])

  return (
    <Routes>
      <Route path="/w/:workspace/*" element={<WorkspaceShell home={home ?? 'default'} />} />
      <Route path="*" element={<ToWorkspace home={home} />} />
    </Routes>
  )
}

// "/", "/code?repo=1", "/trace/12" (links from before workspaces) → the process workspace.
function ToWorkspace({ home }: { home: string | undefined }) {
  const { pathname, search } = useLocation()
  if (home === undefined) return null
  const page = pathname === '/' ? '/dashboard' : pathname
  return <Navigate replace to={`/w/${home}${page}${search}`} />
}

function WorkspaceShell({ home }: { home: string }) {
  const { workspace = '' } = useParams()
  const { pathname, search } = useLocation()
  // A hand-typed name off the rule would make the header throw; the process workspace may
  // predate the rule.
  const usable = WORKSPACE_NAME.test(workspace) || workspace === home
  // Set during render: the pages below fetch in their effects, which run before this
  // component's own effects.
  setWorkspace(usable ? workspace : null)
  // undefined while loading, null when the API cannot be reached
  const [workspaces, setWorkspaces] = useState<Workspace[] | null | undefined>(undefined)

  // undefined while loading, null when /me could not be read (then the API alone decides)
  const [me, setMe] = useState<Me | null | undefined>(undefined)
  const [signedOut, setSignedOut] = useState(false) // /me said 401: no session and no key

  useEffect(() => {
    listWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces(null))
    getMe()
      .then(setMe)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) setSignedOut(true)
        setMe(null)
      })
  }, [])

  useEffect(() => {
    document.title = `${workspace} · enigram`
    return () => {
      document.title = 'enigram'
    }
  }, [workspace])

  // When the list could not be loaded, let the pages show their own errors.
  const known = usable && (workspaces === null || !!workspaces?.some((w) => w.name === workspace))

  if (signedOut) return <SignIn />

  // /w/<ws>/<page>/...: the page shown in the top bar.
  const segment = pathname.split('/')[3] ?? ''
  const page = PAGE_LABELS[segment] ?? ''
  const item = itemOf(segment, pathname, search)

  return (
    <AccessProvider value={accessOf(me, workspace)}>
      <SidebarProvider>
        {/* The first Tab stop: past the sidebar and the top bar, straight to the page (U4). */}
        <a
          href="#content"
          className="sr-only z-50 rounded-md bg-background px-3 py-2 text-sm focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:ring-[3px] focus:ring-ring/50"
        >
          Skip to content
        </a>
        <AppSidebar
          me={me}
          workspace={workspace}
          workspaces={workspaces}
          onCreated={(created) => setWorkspaces([...(workspaces ?? []), created])}
        />
        <SidebarInset>
          <header className="sticky top-0 z-10 flex h-12 shrink-0 items-center gap-2 border-b bg-background/90 px-4 backdrop-blur">
            <SidebarTrigger className="-ml-1" />
            <Separator orientation="vertical" className="mr-1 data-[orientation=vertical]:h-4" />
            <nav aria-label="Breadcrumb" className="min-w-0 text-sm">
              <ol className="m-0 flex min-w-0 list-none items-center gap-1.5 p-0 text-muted-foreground">
                <li className="shrink-0">
                  <Link to={`/w/${workspace}/dashboard`} className="text-muted-foreground">
                    {workspace}
                  </Link>
                </li>
                {page && (
                  <li className="flex min-w-0 items-center gap-1.5">
                    <span aria-hidden> / </span>
                    {item ? (
                      <Link to={`/w/${workspace}/${segment}`} className="text-muted-foreground">
                        {page}
                      </Link>
                    ) : (
                      <span aria-current="page" className="text-foreground">
                        {page}
                      </span>
                    )}
                  </li>
                )}
                {item && (
                  <li className="flex min-w-0 items-center gap-1.5">
                    <span aria-hidden> / </span>
                    <span aria-current="page" className="truncate text-foreground">
                      {item}
                    </span>
                  </li>
                )}
              </ol>
            </nav>
            <CommandPalette workspace={workspace} workspaces={workspaces ?? []} />
          </header>
          <div
            id="content"
            tabIndex={-1}
            className="w-full max-w-[1140px] px-4 pt-6 pb-20 outline-none md:px-8"
          >
            {workspaces === undefined ? null : !known ? (
              <Empty>
                No workspace “{workspace}”. <Link to={`/w/${home}/dashboard`}>Open {home}</Link>
              </Empty>
            ) : (
              // Keyed: a switch remounts every page, so none keeps the old workspace's data.
              <Routes key={workspace}>
                <Route index element={<Navigate to="dashboard" replace />} />
                <Route path="documents" element={<Documents />} />
                <Route path="documents/repos/:id" element={<RepoDetail />} />
                {/* The Ask page became the Auto agent of Chat (2026-11-08). */}
                <Route path="ask" element={<Moved to="chat" />} />
                <Route path="chat/:id?" element={<Chat />} />
                <Route path="knowledge" element={<Knowledge />} />
                {/* Before item 5 A2: Code and Annotations were pages of their own. */}
                <Route path="code" element={<Moved to="knowledge" />} />
                <Route path="annotations" element={<Moved to="knowledge" tab="notes" />} />
                <Route path="review" element={<Review />} />
                <Route path="trace" element={<TraceList />} />
                <Route path="trace/:id" element={<TraceDetail />} />
                <Route path="graph" element={<Graph />} />
                <Route path="dashboard" element={<Dashboard />} />
                {/* Before 2026-11-12 a repository's page was under the Dashboard. */}
                <Route path="dashboard/repos/:id" element={<RepoMoved />} />
                <Route path="memory" element={<Memory />} />
                <Route path="members" element={<Members />} />
                <Route path="agents" element={<Agents />} />
                <Route path="setup" element={<Setup />} />
                <Route path="*" element={<Empty>Not found.</Empty>} />
              </Routes>
            )}
          </div>
        </SidebarInset>
      </SidebarProvider>
      <Toaster />
    </AccessProvider>
  )
}

import { useEffect, useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation, useParams } from 'react-router'
import { getHealth, listWorkspaces, setWorkspace, type Workspace } from './api'
import AppSidebar, { NAV } from './components/AppSidebar'
import Empty from './components/Empty'
import { Separator } from './components/ui/separator'
import { SidebarInset, SidebarProvider, SidebarTrigger } from './components/ui/sidebar'
import Annotations from './pages/Annotations'
import Ask from './pages/Ask'
import Code from './pages/Code'
import Documents from './pages/Documents'
import Graph from './pages/Graph'
import { TraceDetail, TraceList } from './pages/Trace'
import { WORKSPACE_NAME } from './workspace'

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
  const page = pathname === '/' ? '/documents' : pathname
  return <Navigate replace to={`/w/${home}${page}${search}`} />
}

function WorkspaceShell({ home }: { home: string }) {
  const { workspace = '' } = useParams()
  const { pathname } = useLocation()
  // A hand-typed name off the rule would make the header throw; the process workspace may
  // predate the rule.
  const usable = WORKSPACE_NAME.test(workspace) || workspace === home
  // Set during render: the pages below fetch in their effects, which run before this
  // component's own effects.
  setWorkspace(usable ? workspace : null)
  // undefined while loading, null when the API cannot be reached
  const [workspaces, setWorkspaces] = useState<Workspace[] | null | undefined>(undefined)

  useEffect(() => {
    listWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces(null))
  }, [])

  useEffect(() => {
    document.title = `${workspace} · enigram`
    return () => {
      document.title = 'enigram'
    }
  }, [workspace])

  // When the list could not be loaded, let the pages show their own errors.
  const known = usable && (workspaces === null || !!workspaces?.some((w) => w.name === workspace))

  // /w/<ws>/<page>/...: the page shown in the top bar.
  const page = PAGE_LABELS[pathname.split('/')[3] ?? ''] ?? ''

  return (
    <SidebarProvider>
      <AppSidebar
        workspace={workspace}
        workspaces={workspaces}
        onCreated={(created) => setWorkspaces([...(workspaces ?? []), created])}
      />
      <SidebarInset>
        <header className="sticky top-0 z-10 flex h-12 shrink-0 items-center gap-2 border-b bg-background/90 px-4 backdrop-blur">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="mr-1 data-[orientation=vertical]:h-4" />
          <span className="truncate text-sm text-muted-foreground">
            {workspace}
            {page && <span className="text-foreground"> / {page}</span>}
          </span>
        </header>
        <div className="w-full max-w-[1140px] px-4 pt-6 pb-20 md:px-8">
          {workspaces === undefined ? null : !known ? (
            <Empty>
              No workspace “{workspace}”. <Link to={`/w/${home}/documents`}>Open {home}</Link>
            </Empty>
          ) : (
            // Keyed: a switch remounts every page, so none keeps the old workspace's data.
            <Routes key={workspace}>
              <Route index element={<Navigate to="documents" replace />} />
              <Route path="documents" element={<Documents />} />
              <Route path="ask" element={<Ask />} />
              <Route path="code" element={<Code />} />
              <Route path="trace" element={<TraceList />} />
              <Route path="trace/:id" element={<TraceDetail />} />
              <Route path="annotations" element={<Annotations />} />
              <Route path="graph" element={<Graph />} />
              <Route path="*" element={<Empty>Not found.</Empty>} />
            </Routes>
          )}
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}

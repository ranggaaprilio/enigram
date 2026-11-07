import { useEffect, useState } from 'react'
import { Link, Navigate, NavLink, Route, Routes, useLocation, useParams } from 'react-router'
import { getHealth, listWorkspaces, setWorkspace, type Workspace } from './api'
import Empty from './components/Empty'
import StatusBadge from './components/StatusBadge'
import WorkspacePicker from './components/WorkspacePicker'
import { cn } from './lib/utils'
import Annotations from './pages/Annotations'
import Ask from './pages/Ask'
import Code from './pages/Code'
import Documents from './pages/Documents'
import Graph from './pages/Graph'
import { TraceDetail, TraceList } from './pages/Trace'
import { useWorkspacePath, WORKSPACE_NAME } from './workspace'

const PAGES = [
  ['/documents', 'Documents'],
  ['/ask', 'Ask'],
  ['/code', 'Code'],
  ['/trace', 'Trace'],
  ['/annotations', 'Annotations'],
] as const

// Selected tab = one surface lift, not a colour (DESIGN.md pricing-tab).
const tab = ({ isActive }: { isActive: boolean }) =>
  cn(
    'rounded-full px-3 py-1.5 text-sm font-medium whitespace-nowrap text-muted-foreground hover:text-foreground hover:no-underline',
    isActive && 'bg-secondary text-foreground',
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
  const path = useWorkspacePath()
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

  return (
    <>
      <header className="sticky top-0 z-10 border-b bg-background/90 backdrop-blur">
        <div className="mx-auto flex min-h-14 max-w-[1140px] items-center gap-5 px-4">
          <span className="shrink-0 text-base font-semibold tracking-[-0.4px]">enigram</span>
          <nav aria-label="Pages" className="flex gap-1 overflow-x-auto [scrollbar-width:none]">
            {PAGES.map(([to, label]) => (
              <NavLink key={to} to={path(to)} className={tab}>
                {label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto text-[13px] whitespace-nowrap">
            {workspaces === null ? (
              <StatusBadge status="failed">API unreachable</StatusBadge>
            ) : (
              workspaces && (
                <WorkspacePicker
                  current={workspace}
                  workspaces={workspaces}
                  onCreated={(created) => setWorkspaces([...workspaces, created])}
                />
              )
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-[1140px] px-4 pt-8 pb-20">
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
            {/* No nav link: structural LightRAG fixes only, linked from the Annotations page. */}
            <Route path="graph" element={<Graph />} />
            <Route path="*" element={<Empty>Not found.</Empty>} />
          </Routes>
        )}
      </main>
    </>
  )
}

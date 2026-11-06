import { useEffect, useState } from 'react'
import { Navigate, NavLink, Route, Routes } from 'react-router'
import { getHealth } from './api'
import Empty from './components/Empty'
import StatusBadge from './components/StatusBadge'
import { cn } from './lib/utils'
import Annotations from './pages/Annotations'
import Ask from './pages/Ask'
import Documents from './pages/Documents'
import Graph from './pages/Graph'
import { TraceDetail, TraceList } from './pages/Trace'

const PAGES = [
  ['/documents', 'Documents'],
  ['/ask', 'Ask'],
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
  // undefined while loading, null when the API cannot be reached
  const [workspace, setWorkspace] = useState<string | null | undefined>(undefined)

  useEffect(() => {
    getHealth()
      .then((health) => setWorkspace(health.workspace))
      .catch(() => setWorkspace(null))
  }, [])

  return (
    <>
      <header className="sticky top-0 z-10 border-b bg-background/90 backdrop-blur">
        <div className="mx-auto flex min-h-14 max-w-[1140px] items-center gap-5 px-4">
          <span className="shrink-0 text-base font-semibold tracking-[-0.4px]">enigram</span>
          <nav aria-label="Pages" className="flex gap-1 overflow-x-auto [scrollbar-width:none]">
            {PAGES.map(([to, label]) => (
              <NavLink key={to} to={to} className={tab}>
                {label}
              </NavLink>
            ))}
          </nav>
          <span className="ml-auto text-[13px] whitespace-nowrap text-muted-foreground">
            {workspace === null ? (
              <StatusBadge status="failed">API unreachable</StatusBadge>
            ) : (
              workspace && <span className="max-md:hidden">workspace · {workspace}</span>
            )}
          </span>
        </div>
      </header>
      <main className="mx-auto max-w-[1140px] px-4 pt-8 pb-20">
        <Routes>
          <Route path="/" element={<Navigate to="/documents" replace />} />
          <Route path="/documents" element={<Documents />} />
          <Route path="/ask" element={<Ask />} />
          <Route path="/trace" element={<TraceList />} />
          <Route path="/trace/:id" element={<TraceDetail />} />
          <Route path="/annotations" element={<Annotations />} />
          {/* No nav link: structural LightRAG fixes only, linked from the Annotations page. */}
          <Route path="/graph" element={<Graph />} />
          <Route path="*" element={<Empty>Not found.</Empty>} />
        </Routes>
      </main>
    </>
  )
}

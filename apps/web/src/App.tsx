import { useEffect, useState } from 'react'
import { Navigate, NavLink, Route, Routes } from 'react-router'
import { getHealth } from './api'
import Annotations from './pages/Annotations'
import Ask from './pages/Ask'
import Documents from './pages/Documents'
import Graph from './pages/Graph'
import { TraceDetail, TraceList } from './pages/Trace'

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
      <header className="topbar">
        <div className="topbar-inner">
          <span className="brand">enigram</span>
          <nav>
            <NavLink to="/documents">Documents</NavLink>
            <NavLink to="/ask">Ask</NavLink>
            <NavLink to="/trace">Trace</NavLink>
            <NavLink to="/annotations">Annotations</NavLink>
          </nav>
          <span className={workspace === null ? 'workspace error-text' : 'workspace'}>
            {workspace === null ? 'API unreachable' : workspace && `workspace: ${workspace}`}
          </span>
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/documents" replace />} />
          <Route path="/documents" element={<Documents />} />
          <Route path="/ask" element={<Ask />} />
          <Route path="/trace" element={<TraceList />} />
          <Route path="/trace/:id" element={<TraceDetail />} />
          <Route path="/annotations" element={<Annotations />} />
          {/* No nav link: structural LightRAG fixes only, linked from the Annotations page. */}
          <Route path="/graph" element={<Graph />} />
          <Route path="*" element={<p>Not found.</p>} />
        </Routes>
      </main>
    </>
  )
}

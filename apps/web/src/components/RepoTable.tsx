import { useEffect, useState } from 'react'
import { getRepoStats, messageOf, type RepoStat } from '../api'
import { formatAgo } from '../format'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
import { Commit, RemoteBadge, RunOutcome, UPDATES_LABEL } from '@/components/RepoState'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { WsLink } from '@/workspace'

const number = (n: number) => n.toLocaleString('en-US')

// Plan item 6, D2b: each repository's commit, last index and state against its remote; a name
// opens its detail page under Documents. A new `refresh` reloads it.
export default function RepoTable({ refresh = 0 }: { refresh?: number }) {
  const [repos, setRepos] = useState<RepoStat[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    getRepoStats()
      .then((r) => {
        if (!current) return
        setRepos(r)
        setError(null)
      })
      .catch((e) => current && setError(messageOf(e)))
    return () => {
      current = false
    }
  }, [refresh])

  if (error) return <ErrorAlert>{error}</ErrorAlert>
  if (!repos) return <Loading rows={2} />
  if (repos.length === 0) return <p className="m-0 text-muted-foreground">No repository indexed.</p>
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Repository</TableHead>
          <TableHead>Commit</TableHead>
          <TableHead>Indexed</TableHead>
          <TableHead>Remote</TableHead>
          <TableHead>Last index</TableHead>
          <TableHead className="text-right">Size</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {repos.map((r) => (
          <TableRow key={r.id}>
            <TableCell>
              <WsLink to={`/documents/repos/${r.id}`}>{r.name}</WsLink>
              <div className="text-[12px] text-muted-foreground">
                {UPDATES_LABEL[r.updates]} updates
              </div>
            </TableCell>
            <TableCell>
              <Commit repo={r} />
            </TableCell>
            <TableCell className="whitespace-nowrap">{formatAgo(r.indexed_at)}</TableCell>
            <TableCell>
              <RemoteBadge repo={r} />
            </TableCell>
            <TableCell>
              <RunOutcome run={r.last_run} />
            </TableCell>
            <TableCell className="text-right text-[12px] whitespace-nowrap text-muted-foreground tabular-nums">
              {number(r.files)} files · {number(r.symbols)} symbols
              <div>
                {number(r.entrypoints)} entry points · {number(r.documents)} docs
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

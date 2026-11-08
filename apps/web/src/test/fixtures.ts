import type { RepoStat } from '../api'

// A repository as GET /stats/repos lists it (Dashboard and RepoDetail tests).
export const REPO: RepoStat = {
  id: 4,
  name: 'org-white-board',
  url: 'https://github.com/org/white-board',
  ref: null,
  indexed_sha: 'd0eebac9f3e4',
  commit_at: '2026-04-18T21:21:03+07:00',
  indexed_at: '2026-11-07T13:17:54Z',
  remote: { status: 'behind', sha: 'ffffffff', checked_at: '2026-11-08T09:00:00Z', error: null },
  updates: 'poll',
  last_run: null,
  files: 78,
  symbols: 213,
  entrypoints: 7,
  documents: 2,
}

import { useState, type ChangeEvent, type DragEvent, type FormEvent } from 'react'
import { toast } from 'sonner'
import { Check, CircleAlert, FileUp, GitBranch, Loader2, Upload as UploadIcon } from 'lucide-react'
import { indexRepo, messageOf, type Job } from '../../api'
import ErrorAlert from '@/components/ErrorAlert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { cn } from '@/lib/utils'
import { repoLabel } from './sources'

export interface Upload {
  name: string
  state: 'pending' | 'done' | 'error'
  message: string
  book?: string
  jobId?: number
}

type Tab = 'repo' | 'files'

const TABS: { id: Tab; label: string; icon: typeof GitBranch }[] = [
  { id: 'repo', label: 'Git repository', icon: GitBranch },
  { id: 'files', label: 'Upload files', icon: UploadIcon },
]

// Adding a source: a Git repository to keep in sync, or PDF and Markdown files.
export default function AddSourceSheet({
  open,
  onOpenChange,
  canSetToken,
  uploads,
  onFiles,
  onRepoQueued,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  canSetToken: boolean // a private repository's token: admins (plans/rbac.md)
  uploads: Upload[]
  onFiles: (files: File[]) => void
  onRepoQueued: (job: Job, url: string) => void
}) {
  const [tab, setTab] = useState<Tab>('repo')
  const [url, setUrl] = useState('')
  const [ref, setRef] = useState('')
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [dragging, setDragging] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    const repo = url.trim()
    setSending(true)
    try {
      const { job } = await indexRepo(repo, ref.trim() || undefined, token.trim() || undefined)
      setError(null)
      setUrl('')
      setRef('')
      setToken('')
      toast.success(`Syncing ${repoLabel(repo)}`)
      onRepoQueued(job, repo)
      onOpenChange(false)
    } catch (e) {
      setError(messageOf(e))
    } finally {
      setSending(false)
    }
  }

  function onPick(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? [])
    event.target.value = ''
    if (files.length) onFiles(files)
  }

  function onDrop(event: DragEvent) {
    event.preventDefault()
    setDragging(false)
    const files = Array.from(event.dataTransfer.files)
    if (files.length) onFiles(files)
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-md">
        <SheetHeader className="border-b">
          <SheetTitle>Add source</SheetTitle>
          <SheetDescription>
            Sources are indexed by the worker and become searchable on the Chat page.
          </SheetDescription>
        </SheetHeader>

        <div className="flex flex-col gap-5 p-4">
          <div
            role="tablist"
            aria-label="Source type"
            className="grid grid-cols-2 gap-1 rounded-md bg-card p-1"
          >
            {TABS.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                role="tab"
                id={`add-tab-${id}`}
                aria-controls={`add-panel-${id}`}
                aria-selected={tab === id}
                onClick={() => setTab(id)}
                className={cn(
                  'flex items-center justify-center gap-2 rounded-[6px] px-3 py-1.5 text-sm text-muted-foreground transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/25 focus-visible:outline-none',
                  tab === id && 'bg-background font-medium text-foreground shadow-xs',
                )}
              >
                <Icon className="size-4" aria-hidden /> {label}
              </button>
            ))}
          </div>

          {tab === 'repo' ? (
            <form
              id="add-panel-repo"
              role="tabpanel"
              aria-labelledby="add-tab-repo"
              className="flex flex-col gap-4"
              onSubmit={submit}
            >
              <p className="m-0 text-[13px] text-muted-foreground">
                Markdown files on the branch are indexed, and TypeScript code gets a code graph. The
                repository is kept in sync after this first run.
              </p>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="repo-url">Repository URL</Label>
                <Input
                  id="repo-url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://github.com/org/handbook"
                  autoFocus
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="repo-ref">Branch or tag</Label>
                <Input
                  id="repo-ref"
                  value={ref}
                  onChange={(e) => setRef(e.target.value)}
                  placeholder="default branch"
                />
              </div>
              {canSetToken && (
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="repo-token">Access token</Label>
                  <Input
                    id="repo-token"
                    type="password"
                    autoComplete="off"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    placeholder="only for a private repository"
                  />
                  <p className="m-0 text-[12px] text-muted-foreground">
                    Read-only access is enough; it is stored encrypted.
                  </p>
                </div>
              )}
              {error && <ErrorAlert>{error}</ErrorAlert>}
              <Button type="submit" disabled={!url.trim() || sending} className="self-start">
                {sending ? <Loader2 className="animate-spin" /> : <GitBranch />} Start sync
              </Button>
            </form>
          ) : (
            <div
              id="add-panel-files"
              role="tabpanel"
              aria-labelledby="add-tab-files"
              className="flex flex-col gap-4"
            >
              <label
                className={cn(
                  'flex min-h-44 cursor-pointer flex-col items-center justify-center gap-1.5 rounded-md border-[1.5px] border-dashed border-input bg-card px-5 py-8 text-center text-muted-foreground transition-colors hover:border-ring has-[input:focus-visible]:ring-[3px] has-[input:focus-visible]:ring-ring/25',
                  dragging && 'border-ring bg-info-bg',
                )}
                onDragOver={(e) => {
                  e.preventDefault()
                  setDragging(true)
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={onDrop}
              >
                <input
                  type="file"
                  multiple
                  accept=".pdf,.md,.markdown"
                  className="sr-only"
                  onChange={onPick}
                />
                <FileUp className="mb-1 size-6" aria-hidden />
                <strong className="text-base font-medium text-foreground">
                  Drop PDF or Markdown files here
                </strong>
                <span>
                  or <span className="text-link">browse</span> your computer
                </span>
              </label>

              {uploads.length > 0 && (
                <ul aria-label="Uploads" className="m-0 flex list-none flex-col gap-1.5 p-0">
                  {uploads.map((u) => (
                    <li
                      key={u.name}
                      className="flex items-start gap-2 rounded-md bg-card px-3 py-2 text-sm"
                    >
                      {u.state === 'pending' ? (
                        <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin text-info" />
                      ) : u.state === 'done' ? (
                        <Check className="mt-0.5 size-4 shrink-0 text-success" />
                      ) : (
                        <CircleAlert className="mt-0.5 size-4 shrink-0 text-destructive" />
                      )}
                      <span className="min-w-0">
                        <span className="block truncate font-medium">{u.name}</span>
                        <span
                          className={cn(
                            'block text-[13px]',
                            u.state === 'error'
                              ? 'error-text text-destructive'
                              : 'text-muted-foreground',
                          )}
                        >
                          {u.message}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

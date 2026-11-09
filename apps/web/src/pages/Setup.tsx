import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useParams } from 'react-router'
import { useAccess } from '../access'
import {
  getSetup,
  listRepos,
  messageOf,
  rotateWebhookSecret,
  type RepoRow,
  type SetupRow,
} from '../api'
import CopyBlock from '@/components/CopyBlock'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import Loading from '@/components/Loading'
import PageHeader from '@/components/PageHeader'
import StatusBadge from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { WsLink } from '@/workspace'

const muted = 'm-0 text-[13px] text-muted-foreground'
const ADDRESS_KEY = 'enigram-api-address'

// MCP clients and GitHub talk to the API itself, not through the web UI's /api proxy: with
// ENIGRAM_WEB_KEY set, nginx replaces their Authorization header with the UI's key.
function defaultAddress(): string {
  return `${window.location.protocol}//${window.location.hostname}:8080`
}

function useApiAddress(): [string, (value: string) => void] {
  const [address, setAddress] = useState(() => {
    try {
      return localStorage.getItem(ADDRESS_KEY) || defaultAddress()
    } catch {
      return defaultAddress()
    }
  })
  const save = (value: string) => {
    setAddress(value)
    try {
      localStorage.setItem(ADDRESS_KEY, value)
    } catch {
      // kept for this visit only
    }
  }
  return [address, save]
}

// Only a plain http(s) address goes into the commands: they are pasted into a shell.
const ADDRESS = /^https?:\/\/[A-Za-z0-9.-]+(:[0-9]+)?(\/[A-Za-z0-9._~%/-]*)?$/

function baseOf(address: string): string | null {
  const trimmed = address.trim().replace(/\/+$/, '')
  return ADDRESS.test(trimmed) ? trimmed : null
}

function SetBadge({ set }: { set: boolean }) {
  return set ? (
    <StatusBadge status="done">Set</StatusBadge>
  ) : (
    <StatusBadge status="unset">Not set</StatusBadge>
  )
}

function Section(props: { title: string; hint: ReactNode; children: ReactNode }) {
  return (
    <section className="mt-10 flex flex-col gap-3">
      <h2 className="m-0">{props.title}</h2>
      <p className={muted}>{props.hint}</p>
      {props.children}
    </section>
  )
}

// Settings → Setup (admins): connect agents over MCP, set up repository webhooks, and see what
// the API's .env sets. Keys for people and agents stay on Members.
export default function Setup() {
  const { workspace = '' } = useParams()
  const access = useAccess()
  const [address, setAddress] = useApiAddress()
  const typed = baseOf(address)
  const base = typed ?? defaultAddress()

  if (!access.can('admin')) return <Empty>Only an admin of {workspace} sets it up.</Empty>

  const mcpUrl = `${base}/mcp/w/${workspace}`
  return (
    <section>
      <PageHeader
        title="Setup"
        description={`Connect agents to ${workspace}, re-index repositories on every push, and check what the server runs with.`}
      />
      <div className="flex max-w-md flex-col gap-2">
        <Label htmlFor="api-address">API address</Label>
        <Input
          id="api-address"
          value={address}
          aria-invalid={typed === null}
          onChange={(e) => setAddress(e.target.value)}
        />
        {typed === null && (
          <p role="alert" className="m-0 text-[13px] text-destructive">
            Not an http(s) address: the commands below use {base}.
          </p>
        )}
        <p className={muted}>
          Where agents and GitHub reach the API: port 8080 by default, or the address in front of
          it. Kept in this browser.
        </p>
      </div>

      <Section
        title="Connect an agent (MCP)"
        hint={
          <>
            Run this where the agent works, with a key of this workspace in place of{' '}
            <code>&lt;key&gt;</code>. <WsLink to="/members">Make a key on Members</WsLink>; it is
            shown once.
          </>
        }
      >
        <CopyBlock label="MCP URL" text={mcpUrl} />
        <CopyBlock
          label="Claude Code command"
          text={`claude mcp add --transport http enigram-${workspace} ${mcpUrl} --header "Authorization: Bearer <key>"`}
        />
      </Section>

      <Webhooks base={base} />
      {/* The whole server's settings: an owner's only (GET /setup). */}
      {access.owner && <ServerSettings />}
    </section>
  )
}

function Webhooks({ base }: { base: string }) {
  const [repos, setRepos] = useState<RepoRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<number | null>(null)
  const [made, setMade] = useState<{ repo: RepoRow; secret: string } | null>(null)
  // One rotation at a time: a second would void the secret the first shows. A ref, so two clicks
  // before the next render still send one request.
  const busy = useRef(false)

  const load = useCallback(async () => {
    try {
      setRepos(await listRepos())
    } catch (e) {
      setError(messageOf(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function rotate(repo: RepoRow) {
    if (busy.current) return
    busy.current = true
    try {
      const { secret } = await rotateWebhookSecret(repo.id)
      setMade({ repo, secret })
      setError(null)
    } catch (e) {
      setError(messageOf(e))
    }
    busy.current = false
    setConfirming(null)
    await load()
  }

  return (
    <Section
      title="Webhooks"
      hint="A push to a repository's branch re-indexes it at once instead of at the next poll. Each repository has its own secret."
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {made && (
        <div
          role="status"
          aria-label={`Webhook for ${made.repo.name}`}
          className="flex flex-col gap-2 rounded-md border p-4"
        >
          <p className="m-0 font-medium">Webhook for {made.repo.name}, secret shown only now</p>
          <CopyBlock label="Payload URL" text={`${base}/repos/${made.repo.id}/webhook`} />
          <CopyBlock label="Secret" text={made.secret} />
          <p className={muted}>
            GitHub: Settings → Webhooks → Add webhook, the payload URL, content type
            application/json, this secret, push events only. GitLab: Settings → Webhooks, the same
            URL, the secret as its token, push events. A laptop needs a tunnel to port 8080
            (cloudflared or ngrok).
          </p>
          <div>
            <Button type="button" size="sm" variant="secondary" onClick={() => setMade(null)}>
              Done
            </Button>
          </div>
        </div>
      )}
      {repos === null ? (
        !error && <Loading rows={2} />
      ) : repos.length === 0 ? (
        <Empty action={<WsLink to="/documents">Index a repository on Documents</WsLink>}>
          No repositories yet.
        </Empty>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Repository</TableHead>
              <TableHead>Branch</TableHead>
              <TableHead>Secret</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {repos.map((repo) => (
              <TableRow key={repo.id}>
                <TableCell>{repo.name}</TableCell>
                <TableCell>{repo.ref ?? 'default'}</TableCell>
                <TableCell>
                  <SetBadge set={repo.webhook} />
                </TableCell>
                <TableCell>
                  {confirming === repo.id ? (
                    <span className="flex flex-wrap items-center gap-2">
                      <Button type="button" size="sm" onClick={() => void rotate(repo)}>
                        {repo.webhook
                          ? `Rotate secret of ${repo.name}`
                          : `Make secret for ${repo.name}`}
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        onClick={() => setConfirming(null)}
                      >
                        Cancel
                      </Button>
                      {repo.webhook && (
                        <span className={muted}>The current secret stops working.</span>
                      )}
                    </span>
                  ) : (
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      onClick={() => setConfirming(repo.id)}
                    >
                      {repo.webhook ? 'Rotate secret' : 'Make secret'}
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Section>
  )
}

function ServerSettings() {
  const [rows, setRows] = useState<SetupRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getSetup()
      .then((s) => setRows(s.settings))
      .catch((e) => setError(messageOf(e)))
  }, [])

  const groups = [...new Set((rows ?? []).map((r) => r.group))]
  return (
    <Section
      title="Server settings"
      hint="Read from the server's .env when the API starts. To change one, edit .env and restart the API and the worker. Secrets are never shown."
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {rows === null && !error && <Loading rows={4} />}
      {groups.map((group) => (
        <div key={group} className="flex flex-col gap-2">
          <h3 className="m-0 text-sm font-medium">{group}</h3>
          <Table aria-label={group}>
            <TableHeader>
              <TableRow>
                <TableHead>Setting</TableHead>
                <TableHead>Variable</TableHead>
                <TableHead>Value</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(rows ?? [])
                .filter((r) => r.group === group)
                .map((r) => (
                  <TableRow key={r.env}>
                    <TableCell>{r.label}</TableCell>
                    <TableCell>
                      <code className="text-[13px]">{r.env}</code>
                    </TableCell>
                    <TableCell>
                      {r.secret || r.value === null ? <SetBadge set={r.set} /> : r.value}
                    </TableCell>
                  </TableRow>
                ))}
            </TableBody>
          </Table>
        </div>
      ))}
    </Section>
  )
}

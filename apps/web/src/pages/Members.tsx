import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { toast } from 'sonner'
import { useParams } from 'react-router'
import { useAccess } from '../access'
import {
  createKey,
  getAuthConfig,
  inviteMember,
  listMembers,
  messageOf,
  removeMember,
  revokeKey,
  setMember,
  type Member,
  type Role,
} from '../api'
import CopyBlock from '@/components/CopyBlock'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import PageHeader from '@/components/PageHeader'
import StatusBadge from '@/components/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

const ROLES: { value: Role; label: string }[] = [
  { value: 'viewer', label: 'Viewer: read and ask' },
  { value: 'contributor', label: 'Contributor: + propose notes, review' },
  { value: 'maintainer', label: 'Maintainer: + verify, documents, repositories' },
  { value: 'admin', label: 'Admin: + members, keys, tokens' },
]

function RoleSelect(props: {
  id?: string
  label?: string
  value: Role
  onChange: (role: Role) => void
}) {
  return (
    <NativeSelect
      size="sm"
      id={props.id}
      aria-label={props.label}
      value={props.value}
      onChange={(e) => props.onChange(e.target.value as Role)}
    >
      {ROLES.map((r) => (
        <NativeSelectOption key={r.value} value={r.value}>
          {props.id ? r.label : r.value}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  )
}

type Act = (run: () => Promise<unknown>, message?: string) => Promise<boolean>

// plans/rbac.md: the admins of a workspace manage its members. People (sign-in by email) and API
// keys (agents, scripts) are apart: a person is invited, a key is made. Every write reloads.
export default function Members() {
  const { workspace = '' } = useParams()
  const access = useAccess()
  const [members, setMembers] = useState<Member[] | null>(null)
  const [login, setLogin] = useState(false) // People only when sign-in (OIDC) is set
  const [error, setError] = useState<string | null>(null)
  const [invited, setInvited] = useState<string | null>(null)
  const [newKey, setNewKey] = useState<{ name: string; key: string } | null>(null)
  const admin = access.can('admin')

  const load = useCallback(async () => {
    try {
      setMembers(await listMembers())
    } catch (e) {
      setError(messageOf(e))
    }
  }, [])

  useEffect(() => {
    if (!admin) return
    void load()
    getAuthConfig().then(
      (c) => setLogin(c.login),
      () => setLogin(false),
    )
  }, [admin, load])

  // True when the API took it; its refusal is shown above the sections.
  const act: Act = async (run, message) => {
    let done = false
    try {
      await run()
      setError(null)
      if (message) toast.success(message)
      done = true
    } catch (e) {
      setError(messageOf(e))
    }
    await load()
    return done
  }

  if (!admin) return <Empty>Only an admin of {workspace} manages its members.</Empty>

  const people = members?.filter((m) => m.kind === 'user') ?? []
  const keys = members?.filter((m) => m.kind !== 'user') ?? []
  const link = `${window.location.origin}/w/${workspace}`

  return (
    <section>
      <PageHeader
        title="Members"
        description={`Who can use ${workspace}, and with which role. A role includes every right of the ones before it.`}
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}

      {login && (
        <Part
          title="People"
          hint="Invite someone by email, then send them the link. They sign in with that email and get the role you chose. Enigram sends no email itself."
        >
          <AddForm
            title="Invite a person"
            field="Email"
            type="email"
            placeholder="name@company.com"
            submit="Invite"
            onSubmit={(email, role) =>
              act(async () => {
                const made = await inviteMember(email, role)
                setInvited(made.name)
              }, `${email} invited as ${role}`)
            }
          />
          {invited && (
            <div role="status" className="rounded-md border p-4">
              <p className="m-0 font-medium">Send {invited} this link</p>
              <p className="mt-1 mb-2 text-muted-foreground">
                They sign in with {invited}; until then they show as invited.
              </p>
              <CopyBlock label="Workspace link" text={link} />
              <Button
                type="button"
                size="sm"
                variant="secondary"
                className="mt-3"
                onClick={() => setInvited(null)}
              >
                Done
              </Button>
            </div>
          )}
          {members === null ? null : people.length === 0 ? (
            <Empty>Nobody invited yet.</Empty>
          ) : (
            <MemberTable rows={people} me={access.name} act={act} people />
          )}
        </Part>
      )}

      <Part
        title="API keys"
        hint="For agents, MCP clients and scripts. A new key is shown once; Enigram keeps only its hash."
      >
        <AddForm
          title="Make a new key"
          field="Key name"
          placeholder="qa-agent"
          submit="Make key"
          onSubmit={(name, role) =>
            act(async () => {
              const made = await createKey(name, role)
              setNewKey({ name: made.name, key: made.key })
            })
          }
        />
        {newKey && (
          <div role="status" className="rounded-md border p-4">
            <p className="m-0 font-medium">Key for {newKey.name}, shown only now</p>
            <p className="mt-1 mb-2 text-muted-foreground">Copy it to whoever uses it.</p>
            <CopyBlock label="API key" text={newKey.key} />
            <Button
              type="button"
              size="sm"
              variant="secondary"
              className="mt-3"
              onClick={() => setNewKey(null)}
            >
              Done
            </Button>
          </div>
        )}
        {members === null ? null : keys.length === 0 ? (
          <Empty>No keys yet.</Empty>
        ) : (
          <MemberTable rows={keys} me={access.name} act={act} />
        )}
      </Part>
    </section>
  )
}

function Part(props: { title: string; hint: string; children: ReactNode }) {
  return (
    <section aria-label={props.title} className="mt-10 flex flex-col gap-4">
      <div>
        <h2 className="m-0">{props.title}</h2>
        <p className="mt-1 mb-0 max-w-[64ch] text-muted-foreground">{props.hint}</p>
      </div>
      {props.children}
    </section>
  )
}

// People get Remove only; a key can also be revoked (it stops working everywhere).
function MemberTable(props: { rows: Member[]; me: string | null; act: Act; people?: boolean }) {
  const [confirming, setConfirming] = useState<{ name: string; action: 'remove' | 'revoke' }>()

  async function confirm(m: Member, action: 'remove' | 'revoke') {
    await props.act(
      () => (action === 'remove' ? removeMember(m.name) : revokeKey(m.name)),
      action === 'remove' ? `${m.name} removed` : `${m.name}'s key revoked`,
    )
    setConfirming(undefined)
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{props.people ? 'Person' : 'Key'}</TableHead>
          <TableHead>Role</TableHead>
          {props.people && <TableHead>Status</TableHead>}
          <TableHead>Actions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {props.rows.map((m) => (
          <TableRow key={m.name}>
            <TableCell>
              {m.display_name ?? m.name}
              {m.name === props.me && <span className="text-muted-foreground"> (you)</span>}
              {m.owner && (
                <Badge variant="outline" className="ml-2">
                  owner
                </Badge>
              )}
              {m.display_name && <span className="block text-muted-foreground">{m.name}</span>}
            </TableCell>
            <TableCell>
              <RoleSelect
                label={`Role of ${m.name}`}
                value={m.role}
                onChange={(role) =>
                  void props.act(() => setMember(m.name, role), `${m.name} is now ${role}`)
                }
              />
            </TableCell>
            {props.people && (
              <TableCell>
                <StatusBadge status={m.signed_in ? 'active' : 'invited'} />
              </TableCell>
            )}
            <TableCell>
              {confirming?.name === m.name ? (
                <span className="flex flex-wrap items-center gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="soft-destructive"
                    onClick={() => void confirm(m, confirming.action)}
                  >
                    {confirming.action === 'remove' ? `Remove ${m.name}` : `Revoke ${m.name}'s key`}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    onClick={() => setConfirming(undefined)}
                  >
                    Cancel
                  </Button>
                </span>
              ) : (
                <span className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    onClick={() => setConfirming({ name: m.name, action: 'remove' })}
                  >
                    Remove
                  </Button>
                  {!props.people && (
                    <Button
                      type="button"
                      size="sm"
                      variant="soft-destructive"
                      onClick={() => setConfirming({ name: m.name, action: 'revoke' })}
                    >
                      Revoke key
                    </Button>
                  )}
                </span>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function AddForm(props: {
  title: string
  field: string
  type?: string
  placeholder: string
  submit: string
  onSubmit: (value: string, role: Role) => Promise<boolean>
}) {
  const [value, setValue] = useState('')
  const [role, setRole] = useState<Role>('viewer') // new members start as viewers
  const id = props.submit.toLowerCase().replace(/\W+/g, '-')

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (await props.onSubmit(value.trim(), role)) {
      setValue('')
      setRole('viewer')
    }
  }

  return (
    <form
      aria-label={props.title}
      onSubmit={submit}
      className="flex flex-wrap items-end gap-3 rounded-md border p-4"
    >
      <div className="flex min-w-[16rem] flex-1 flex-col gap-1.5">
        <Label htmlFor={`${id}-value`}>{props.field}</Label>
        <Input
          id={`${id}-value`}
          type={props.type ?? 'text'}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={props.placeholder}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${id}-role`}>Role</Label>
        <RoleSelect id={`${id}-role`} value={role} onChange={setRole} />
      </div>
      <Button type="submit" size="sm" disabled={!value.trim()}>
        {props.submit}
      </Button>
    </form>
  )
}

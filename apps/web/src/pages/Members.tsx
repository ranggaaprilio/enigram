import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { toast } from 'sonner'
import { KeyRound, UserPlus } from 'lucide-react'
import { useParams } from 'react-router'
import { useAccess } from '../access'
import {
  createKey,
  listMembers,
  messageOf,
  removeMember,
  revokeKey,
  setMember,
  type Member,
  type Role,
} from '../api'
import Empty from '@/components/Empty'
import ErrorAlert from '@/components/ErrorAlert'
import PageHeader from '@/components/PageHeader'
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

// docs/rbac.md: the admins of a workspace manage its members and keys. Every write reloads.
export default function Members() {
  const { workspace = '' } = useParams()
  const access = useAccess()
  const [members, setMembers] = useState<Member[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<{ name: string; action: 'remove' | 'revoke' }>()
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
    if (admin) void load()
  }, [admin, load])

  // True when the API took it; its refusal is shown above the table.
  async function act(run: () => Promise<unknown>, message?: string): Promise<boolean> {
    let done = false
    try {
      await run()
      setError(null)
      if (message) toast.success(message)
      done = true
    } catch (e) {
      setError(messageOf(e))
    }
    setConfirming(undefined)
    await load()
    return done
  }

  if (!admin) return <Empty>Only an admin of {workspace} manages its members.</Empty>

  return (
    <section>
      <PageHeader
        title="Members"
        description={`Who can use ${workspace}, and with which role. A role includes every right of the ones before it.`}
      />
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {newKey && (
        <div role="status" className="my-4 rounded-md border p-4">
          <p className="m-0 font-medium">Key for {newKey.name}, shown only now</p>
          <p className="mt-1 mb-2 text-muted-foreground">
            Copy it to whoever uses it; Enigram keeps only its hash.
          </p>
          <code className="block rounded-sm bg-card px-3 py-2 text-[13px] [overflow-wrap:anywhere]">
            {newKey.key}
          </code>
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
      {members === null ? null : members.length === 0 ? (
        <Empty>No members yet.</Empty>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.map((m) => (
              <TableRow key={m.name}>
                <TableCell>
                  {m.name}
                  {m.name === access.name && <span className="text-muted-foreground"> (you)</span>}
                  {m.owner && (
                    <Badge variant="outline" className="ml-2">
                      owner
                    </Badge>
                  )}
                </TableCell>
                <TableCell>
                  <RoleSelect
                    label={`Role of ${m.name}`}
                    value={m.role}
                    onChange={(role) =>
                      void act(() => setMember(m.name, role), `${m.name} is now ${role}`)
                    }
                  />
                </TableCell>
                <TableCell>
                  {confirming?.name === m.name ? (
                    <span className="flex flex-wrap items-center gap-2">
                      <Button
                        type="button"
                        size="sm"
                        variant="soft-destructive"
                        onClick={() =>
                          void act(
                            () =>
                              confirming.action === 'remove'
                                ? removeMember(m.name)
                                : revokeKey(m.name),
                            confirming.action === 'remove'
                              ? `${m.name} removed`
                              : `${m.name}'s key revoked`,
                          )
                        }
                      >
                        {confirming.action === 'remove'
                          ? `Remove ${m.name}`
                          : `Revoke ${m.name}'s key`}
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
                      <Button
                        type="button"
                        size="sm"
                        variant="soft-destructive"
                        onClick={() => setConfirming({ name: m.name, action: 'revoke' })}
                      >
                        Revoke key
                      </Button>
                    </span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <div className="mt-8 grid gap-6 md:grid-cols-2">
        <KeyForm
          title="Add an existing key"
          icon={<UserPlus className="size-4" aria-hidden />}
          hint="A key that already works in another workspace."
          submit="Add"
          onSubmit={(name, role) => act(() => setMember(name, role), `${name} added as ${role}`)}
        />
        <KeyForm
          title="Make a new key"
          icon={<KeyRound className="size-4" aria-hidden />}
          hint="For a person or an agent; the key is shown once."
          submit="Make key"
          onSubmit={(name, role) =>
            act(async () => {
              const made = await createKey(name, role)
              setNewKey({ name: made.name, key: made.key })
            })
          }
        />
      </div>
    </section>
  )
}

function KeyForm(props: {
  title: string
  icon: ReactNode
  hint: string
  submit: string
  onSubmit: (name: string, role: Role) => Promise<boolean>
}) {
  const [name, setName] = useState('')
  const [role, setRole] = useState<Role>('viewer') // new members start as viewers
  const id = props.submit.toLowerCase().replace(/\W+/g, '-')

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (await props.onSubmit(name.trim(), role)) {
      setName('')
      setRole('viewer')
    }
  }

  return (
    <form aria-label={props.title} onSubmit={submit} className="flex flex-col gap-2">
      <h2 className="m-0 flex items-center gap-2">
        {props.icon}
        {props.title}
      </h2>
      <p className="m-0 text-muted-foreground">{props.hint}</p>
      <Label htmlFor={`${id}-name`}>Key name</Label>
      <Input
        id={`${id}-name`}
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="qa-team"
      />
      <Label htmlFor={`${id}-role`}>Role</Label>
      <RoleSelect id={`${id}-role`} value={role} onChange={setRole} />
      <div>
        <Button type="submit" size="sm" disabled={!name.trim()}>
          {props.submit}
        </Button>
      </div>
    </form>
  )
}

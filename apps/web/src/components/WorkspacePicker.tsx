import { useState, type FormEvent } from 'react'
import { toast } from 'sonner'
import { useLocation, useNavigate } from 'react-router'
import { Plus } from 'lucide-react'
import ErrorAlert from '@/components/ErrorAlert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { createWorkspace, messageOf, type Workspace } from '../api'
import { useAccess } from '../access'
import { WORKSPACE_NAME, WORKSPACE_RULE } from '../workspace'

export default function WorkspacePicker(props: {
  current: string
  workspaces: Workspace[]
  onCreated: (created: Workspace) => void
}) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const owner = useAccess().owner // docs/rbac.md: only an owner creates workspaces
  // /w/<ws>/<page>/...: a switch opens the same page; ids and queries belong to the old workspace.
  const page = pathname.split('/')[3] || 'documents'
  const names = props.workspaces.map((w) => w.name)
  if (!names.includes(props.current)) names.unshift(props.current)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const wanted = name.trim()
    if (!WORKSPACE_NAME.test(wanted)) return setError(`Use ${WORKSPACE_RULE}.`)
    setBusy(true)
    try {
      const created = await createWorkspace(wanted)
      // Listed before navigating, so the shell knows the new name.
      props.onCreated(created)
      toast.success(`Workspace ${created.name} created`)
      setOpen(false)
      setName('')
      setError(null)
      navigate(`/w/${created.name}/documents`)
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }

  function close() {
    setOpen(false)
    setError(null)
  }

  return (
    <div className="flex flex-col gap-1.5 [&_[data-slot=native-select-wrapper]]:w-full">
      <NativeSelect
        size="sm"
        className="w-full"
        aria-label="Workspace"
        value={props.current}
        onChange={(e) => navigate(`/w/${e.target.value}/${page}`)}
      >
        {names.map((n) => (
          <NativeSelectOption key={n} value={n}>
            {n}
          </NativeSelectOption>
        ))}
      </NativeSelect>
      {owner && (
        <Button
          size="sm"
          variant="ghost"
          className="justify-start text-muted-foreground"
          aria-expanded={open}
          onClick={() => (open ? close() : setOpen(true))}
        >
          <Plus />
          New workspace
        </Button>
      )}
      {open && (
        <form
          aria-label="New workspace"
          onSubmit={submit}
          className="flex flex-col gap-2 rounded-md border bg-background p-3"
        >
          <Label htmlFor="workspace-name">Workspace name</Label>
          <Input
            id="workspace-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="whiteboard"
            autoFocus
          />
          <p className="m-0 text-xs text-muted-foreground">{WORKSPACE_RULE}</p>
          {error && <ErrorAlert>{error}</ErrorAlert>}
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={busy}>
              Create
            </Button>
          </div>
        </form>
      )}
    </div>
  )
}

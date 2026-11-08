import type { ReactNode } from 'react'
import { accessOf, AccessProvider } from '../access'
import type { Role } from '../api'

// The page as a key named `name` with `role` in workspace "default" sees it.
export function asRole(role: Role, ui: ReactNode, name = 'me') {
  const me = {
    principal: { name, kind: 'key', owner: false, default_workspace: 'default' },
    roles: { default: role },
  }
  return <AccessProvider value={accessOf(me, 'default')}>{ui}</AccessProvider>
}

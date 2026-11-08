import { createContext, useContext } from 'react'
import type { Me, Role } from './api'

// What the caller may do in the current workspace (docs/rbac.md), from GET /me. The UI hides
// what the role does not allow; the API still checks every request.
export interface Access {
  name: string | null // the key's name; null without keys or before /me answers
  role: Role
  owner: boolean
  can: (minimum: Role) => boolean
}

const ORDER: Role[] = ['viewer', 'contributor', 'maintainer', 'admin']

const atLeast = (role: Role | undefined, minimum: Role) =>
  role !== undefined && ORDER.indexOf(role) >= ORDER.indexOf(minimum)

// No keys (principal null), /me still loading, or an API without /me: show everything and let
// the API answer.
const FULL: Access = { name: null, role: 'admin', owner: true, can: () => true }

export function accessOf(me: Me | null | undefined, workspace: string): Access {
  if (!me?.principal) return FULL
  const owner = me.principal.owner
  const role = owner ? 'admin' : (me.roles[workspace] as Role | undefined)
  return {
    name: me.principal.name,
    role: role ?? 'viewer',
    owner,
    can: (minimum) => atLeast(role, minimum),
  }
}

const AccessContext = createContext<Access>(FULL)

export const AccessProvider = AccessContext.Provider

export const useAccess = () => useContext(AccessContext)

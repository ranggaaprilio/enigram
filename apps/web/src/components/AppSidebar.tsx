import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router'
import {
  BookOpen,
  Bot,
  Check,
  ChevronsUpDown,
  BookMarked,
  Brain,
  FileText,
  History,
  MessagesSquare,
  Monitor,
  Moon,
  Network,
  Inbox,
  LayoutDashboard,
  Settings,
  Sun,
  LogOut,
  Users,
  type LucideIcon,
} from 'lucide-react'
import StatusBadge from '@/components/StatusBadge'
import WorkspacePicker from '@/components/WorkspacePicker'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  useSidebar,
} from '@/components/ui/sidebar'
import { useAccess } from '../access'
import { getReview, logout, type Me, type Role, type Workspace } from '../api'
import { useTheme, type Theme } from '../theme'
import { useWorkspacePath } from '../workspace'

interface Page {
  to: string
  label: string
  icon: LucideIcon
  minimum?: Role // shown only to this role and above (docs/rbac.md)
}

export const NAV: { label: string; pages: Page[] }[] = [
  // The home page: where / and a workspace switch land.
  {
    label: 'Overview',
    pages: [{ to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard }],
  },
  {
    label: 'Answers',
    pages: [
      { to: '/chat', label: 'Chat', icon: MessagesSquare },
      { to: '/trace', label: 'Trace', icon: History },
    ],
  },
  {
    label: 'Library',
    pages: [
      { to: '/documents', label: 'Documents', icon: FileText },
      { to: '/knowledge', label: 'Knowledge', icon: BookMarked },
      { to: '/graph', label: 'Graph', icon: Network },
      { to: '/memory', label: 'Memory', icon: Brain, minimum: 'contributor' },
    ],
  },
  {
    label: 'Review',
    pages: [{ to: '/review', label: 'Inbox', icon: Inbox }],
  },
  {
    label: 'Settings',
    pages: [
      { to: '/members', label: 'Members', icon: Users, minimum: 'admin' },
      { to: '/agents', label: 'Agents', icon: Bot, minimum: 'admin' },
      { to: '/setup', label: 'Setup', icon: Settings, minimum: 'admin' },
    ],
  },
]

export default function AppSidebar(props: {
  me: Me | null | undefined
  workspace: string
  workspaces: Workspace[] | null | undefined
  onCreated: (created: Workspace) => void
}) {
  const path = useWorkspacePath()
  const { pathname } = useLocation()
  const { isMobile, setOpenMobile } = useSidebar()
  const access = useAccess()
  const waiting = useWaiting(access.can('maintainer'), pathname)
  const groups = NAV.map((g) => ({
    ...g,
    pages: g.pages.filter((p) => !p.minimum || access.can(p.minimum)),
  })).filter((g) => g.pages.length > 0)

  return (
    <Sidebar collapsible="icon">
      {/* A landmark for the app name and the workspace switch, as the pages below have theirs. */}
      <SidebarHeader role="region" aria-label="App and workspace" className="gap-3 p-3">
        <div className="flex items-center gap-2 px-1">
          <BookOpen className="size-4 shrink-0" aria-hidden />
          <span className="font-semibold tracking-[-0.2px] group-data-[collapsible=icon]:hidden">
            enigram
          </span>
        </div>
        <div className="group-data-[collapsible=icon]:hidden">
          {props.workspaces === null ? (
            <StatusBadge status="failed">API unreachable</StatusBadge>
          ) : (
            props.workspaces && (
              <WorkspacePicker
                current={props.workspace}
                workspaces={props.workspaces}
                onCreated={props.onCreated}
              />
            )
          )}
        </div>
      </SidebarHeader>
      <SidebarContent>
        <nav aria-label="Pages">
          {groups.map((group) => (
            <SidebarGroup key={group.label}>
              <SidebarGroupLabel className="text-muted-foreground">{group.label}</SidebarGroupLabel>
              <SidebarMenu>
                {group.pages.map(({ to, label, icon: Icon }) => {
                  const href = path(to)
                  const active = pathname === href || pathname.startsWith(`${href}/`)
                  return (
                    <SidebarMenuItem key={to}>
                      <SidebarMenuButton asChild isActive={active} tooltip={label}>
                        <Link
                          to={href}
                          aria-current={active ? 'page' : undefined}
                          // The drawer closes on a phone once a page is picked.
                          onClick={() => isMobile && setOpenMobile(false)}
                          className="text-sidebar-foreground hover:no-underline"
                        >
                          <Icon aria-hidden />
                          <span>{label}</span>
                        </Link>
                      </SidebarMenuButton>
                      {to === '/review' && waiting > 0 && (
                        <SidebarMenuBadge aria-label={`${waiting} waiting`}>
                          {waiting}
                        </SidebarMenuBadge>
                      )}
                    </SidebarMenuItem>
                  )
                })}
              </SidebarMenu>
            </SidebarGroup>
          ))}
        </nav>
      </SidebarContent>
      <SidebarFooter>
        <UserMenu me={props.me} workspace={props.workspace} />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}

// How many notes wait in the inbox, for the maintainers who decide them; checked again on every
// page change so it drops as they work through it.
function useWaiting(enabled: boolean, pathname: string): number {
  const [waiting, setWaiting] = useState(0)
  useEffect(() => {
    if (!enabled) return
    let current = true
    getReview()
      .then((items) => current && setWaiting(items.length))
      .catch(() => current && setWaiting(0))
    return () => {
      current = false
    }
  }, [enabled, pathname])
  return enabled ? waiting : 0
}

const THEMES: { value: Theme; label: string; icon: LucideIcon }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
]

// `me`: undefined while loading, null when /me could not be read.
function UserMenu({ me, workspace }: { me: Me | null | undefined; workspace: string }) {
  const [theme, setTheme] = useTheme()

  // Without keys the API is open and everyone acts as admin (docs/rbac.md).
  const p = me?.principal
  const name = p ? p.display_name || p.name : me ? 'Local' : 'Account'
  const role = me ? (me.principal ? me.roles[workspace] : 'admin') : undefined
  const detail = p
    ? p.kind === 'user'
      ? `${p.name}${p.owner ? ', owner of every workspace' : ''}`
      : p.owner
        ? 'Owner of every workspace'
        : `API key, ${role ?? 'no role'} in ${workspace}`
    : me
      ? 'No API keys set: open access'
      : null

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        {/* Not modal: a menu need not hide the page from screen readers while it is open. */}
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton size="lg" aria-label="Account and theme" tooltip={name}>
              <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-sidebar-accent text-xs font-semibold uppercase">
                {name.slice(0, 2)}
              </span>
              <span className="grid flex-1 text-left leading-tight">
                <span className="truncate font-medium">{name}</span>
                {role && (
                  <span className="truncate text-xs text-muted-foreground">
                    {role} · {workspace}
                  </span>
                )}
              </span>
              <ChevronsUpDown className="ml-auto" aria-hidden />
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top" align="start" className="min-w-56">
            <DropdownMenuLabel className="font-normal">
              <div className="font-medium">{name}</div>
              {detail && <div className="text-xs text-muted-foreground">{detail}</div>}
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
              Theme
            </DropdownMenuLabel>
            {THEMES.map(({ value, label, icon: Icon }) => (
              <DropdownMenuItem key={value} onSelect={() => setTheme(value)}>
                <Icon aria-hidden />
                {label}
                {theme === value && <Check className="ml-auto" aria-label="selected" />}
              </DropdownMenuItem>
            ))}
            {p?.kind === 'user' && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onSelect={() =>
                    void logout()
                      .catch(() => undefined) // signed out either way: the cookie is gone
                      .finally(() => window.location.assign('/'))
                  }
                >
                  <LogOut aria-hidden />
                  Sign out
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}

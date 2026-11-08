import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router'
import {
  BookOpen,
  Check,
  ChevronsUpDown,
  Code2,
  FileText,
  History,
  MessageSquareText,
  Monitor,
  Moon,
  Network,
  StickyNote,
  Sun,
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
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  useSidebar,
} from '@/components/ui/sidebar'
import { getMe, type Me, type Workspace } from '../api'
import { useTheme, type Theme } from '../theme'
import { useWorkspacePath } from '../workspace'

interface Page {
  to: string
  label: string
  icon: LucideIcon
}

// docs/design.md: Review, Monitor and Settings join as their pages land.
export const NAV: { label: string; pages: Page[] }[] = [
  {
    label: 'Answers',
    pages: [
      { to: '/ask', label: 'Ask', icon: MessageSquareText },
      { to: '/trace', label: 'Trace', icon: History },
    ],
  },
  {
    label: 'Knowledge',
    pages: [
      { to: '/documents', label: 'Documents', icon: FileText },
      { to: '/code', label: 'Code', icon: Code2 },
      { to: '/annotations', label: 'Annotations', icon: StickyNote },
      { to: '/graph', label: 'Graph', icon: Network },
    ],
  },
]

export default function AppSidebar(props: {
  workspace: string
  workspaces: Workspace[] | null | undefined
  onCreated: (created: Workspace) => void
}) {
  const path = useWorkspacePath()
  const { pathname } = useLocation()
  const { isMobile, setOpenMobile } = useSidebar()

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="gap-3 p-3">
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
          {NAV.map((group) => (
            <SidebarGroup key={group.label}>
              <SidebarGroupLabel>{group.label}</SidebarGroupLabel>
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
                    </SidebarMenuItem>
                  )
                })}
              </SidebarMenu>
            </SidebarGroup>
          ))}
        </nav>
      </SidebarContent>
      <SidebarFooter>
        <UserMenu workspace={props.workspace} />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}

const THEMES: { value: Theme; label: string; icon: LucideIcon }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
]

function UserMenu({ workspace }: { workspace: string }) {
  const [theme, setTheme] = useTheme()
  // undefined while loading, null when /me could not be read
  const [me, setMe] = useState<Me | null | undefined>(undefined)

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null))
  }, [])

  // Without keys the API is open and everyone acts as admin (docs/rbac.md).
  const name = me?.principal?.name ?? (me ? 'Local' : 'Account')
  const role = me ? (me.principal ? me.roles[workspace] : 'admin') : undefined
  const detail = me?.principal
    ? me.principal.owner
      ? 'Owner of every workspace'
      : `API key, ${role ?? 'no role'} in ${workspace}`
    : me
      ? 'No API keys set: open access'
      : null

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
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
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}

import type { ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

// One colour per outcome, shared by documents, jobs, queries and annotations.
const TONES: Record<string, string> = {
  queued: 'bg-info-bg text-info',
  running: 'bg-info-bg text-info',
  parsed: 'bg-info-bg text-info',
  deleting: 'bg-info-bg text-info',
  needs_verification: 'bg-info-bg text-info',
  proposed: 'bg-info-bg text-info',
  indexed: 'bg-success-bg text-success',
  done: 'bg-success-bg text-success',
  verified: 'bg-success-bg text-success',
  failed: 'bg-danger-bg text-destructive',
  orphaned: 'bg-danger-bg text-destructive',
  rejected: 'bg-danger-bg text-destructive',
  budget_exceeded: 'bg-warning-bg text-warning',
}

// `status-<status>` stays on the element: the page tests select on it (no stylesheet rule uses it).
export default function StatusBadge({
  status,
  children,
}: {
  status: string
  children?: ReactNode
}) {
  return (
    <Badge
      variant="secondary"
      className={cn(`status status-${status}`, TONES[status] ?? 'text-muted-foreground')}
    >
      {children ?? status.replaceAll('_', ' ')}
    </Badge>
  )
}

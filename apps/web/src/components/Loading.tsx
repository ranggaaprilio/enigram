import { Skeleton } from '@/components/ui/skeleton'

// The one loading state (docs/project/design.md, U2): grey rows where the content will be. The text is
// for screen readers.
export default function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div role="status" className="my-4 flex flex-col gap-2">
      <span className="sr-only">Loading…</span>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  )
}

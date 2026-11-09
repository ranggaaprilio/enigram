import { cn } from '@/lib/utils'

// The enigram mark (docs/design.md): a small graph, three nodes joined, the top one filled, the
// cited answer. Drawn on lucide's 24px grid in the text colour; public/favicon.svg is the same.
export default function Logo({ className }: { className?: string }) {
  return (
    <svg
      data-slot="logo"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden
      className={cn('size-4', className)}
    >
      <path d="M10.1 8.35 6.88 14.7M13.9 8.35l3.22 6.35M9.4 18.8h5.2" />
      <circle cx="12" cy="4.6" r="3.4" fill="currentColor" stroke="none" />
      <circle cx="4.8" cy="18.8" r="2.8" />
      <circle cx="19.2" cy="18.8" r="2.8" />
    </svg>
  )
}

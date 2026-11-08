import type { ReactNode } from 'react'

// The one empty state (docs/design.md, U2): what is missing, and the next step when there is one.
export default function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="my-4 flex flex-col items-center gap-3 rounded-md bg-card px-5 py-10 text-center text-muted-foreground">
      <p className="m-0">{children}</p>
      {action}
    </div>
  )
}

import type { ReactNode } from 'react'

export default function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="my-4 rounded-md bg-card px-5 py-10 text-center text-muted-foreground">
      {children}
    </p>
  )
}

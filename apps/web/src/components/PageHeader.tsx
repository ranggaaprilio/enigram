import type { ReactNode } from 'react'

export default function PageHeader(props: {
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="mb-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="m-0">{props.title}</h1>
        {props.action}
      </div>
      {props.description && (
        <p className="mt-2 max-w-[64ch] text-muted-foreground">{props.description}</p>
      )}
    </div>
  )
}

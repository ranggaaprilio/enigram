import type { ComponentProps } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '@/lib/utils'

// A heading written in a note sits under the page's h1, the Notes h3 and the card: every level
// becomes one small h4, so a "# Title" in a note cannot look like the page title.
function NoteHeading({ children }: ComponentProps<'h1'>) {
  return <h4 className="mt-3 mb-1 text-sm font-semibold first:mt-0">{children}</h4>
}

const NOTE: Components = {
  h1: NoteHeading,
  h2: NoteHeading,
  h3: NoteHeading,
  h4: NoteHeading,
  h5: NoteHeading,
  h6: NoteHeading,
}

// A note written in markdown (notes are short: body size, no 72ch limit of answers).
export default function Markdown({
  children,
  className,
}: {
  children: string
  className?: string
}) {
  return (
    <div
      className={cn('markdown max-w-none text-sm leading-normal [&>*:last-child]:mb-0', className)}
    >
      {/* No images: a note's remote image would be fetched by every reader's browser. */}
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={NOTE} disallowedElements={['img']}>
        {children}
      </ReactMarkdown>
    </div>
  )
}

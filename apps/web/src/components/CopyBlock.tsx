import { toast } from 'sonner'
import { Copy } from 'lucide-react'
import { Button } from '@/components/ui/button'

// A value to paste elsewhere (a URL, a secret), with a button that copies it.
export default function CopyBlock({ label, text }: { label: string; text: string }) {
  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      toast.success('Copied')
    } catch {
      toast.error('Could not copy: select the text instead')
    }
  }
  return (
    <div className="flex items-start gap-2">
      <code
        aria-label={label}
        className="block flex-1 rounded-sm bg-card px-3 py-2 text-[13px] [overflow-wrap:anywhere]"
      >
        {text}
      </code>
      <Button
        type="button"
        size="sm"
        variant="secondary"
        aria-label={`Copy ${label}`}
        onClick={() => void copy()}
      >
        <Copy aria-hidden />
      </Button>
    </div>
  )
}

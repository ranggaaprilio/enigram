import { CircleAlert } from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert'

// `error-box` marks the message itself, so callers and tests can find it by its text.
export default function ErrorAlert({ children }: { children: string }) {
  return (
    <Alert variant="destructive" className="my-4">
      <CircleAlert />
      <AlertDescription className="error-box">{children}</AlertDescription>
    </Alert>
  )
}

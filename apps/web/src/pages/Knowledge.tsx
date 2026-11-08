import { Network } from 'lucide-react'
import { useSearchParams } from 'react-router'
import { useAccess } from '../access'
import PageHeader from '@/components/PageHeader'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { WsLink } from '@/workspace'
import Annotations from './Annotations'
import Code from './Code'

// Plan item 5, A2: one page to read what the code does and the team's notes on it. Explore is
// the former Code page, All notes the former Annotations table; the inbox is its own page
// (Review). `?tab=notes` picks the table; old /code and /annotations links land here.
const TABS = [
  { key: 'explore', label: 'Explore' },
  { key: 'notes', label: 'All notes' },
] as const

export default function Knowledge() {
  const [params] = useSearchParams()
  const access = useAccess()
  const tab = params.get('tab') === 'notes' ? 'notes' : 'explore'

  return (
    <section>
      <PageHeader
        title="Knowledge"
        description="What the code does and what the team knows about it: browse features and files, or every note on code, graph entities and documents."
        action={
          tab === 'notes' &&
          access.can('maintainer') && (
            <Button asChild variant="secondary">
              <WsLink to="/graph" className="hover:no-underline">
                <Network /> Graph editor
              </WsLink>
            </Button>
          )
        }
      />
      <nav aria-label="Knowledge views" className="mb-6 flex gap-1 border-b">
        {TABS.map((t) => (
          <WsLink
            key={t.key}
            to={t.key === 'notes' ? '/knowledge?tab=notes' : '/knowledge'}
            aria-current={tab === t.key ? 'page' : undefined}
            className={cn(
              '-mb-px border-b-2 px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground hover:no-underline',
              tab === t.key ? 'border-foreground text-foreground' : 'border-transparent',
            )}
          >
            {t.label}
          </WsLink>
        ))}
      </nav>
      {tab === 'notes' ? <Annotations embedded /> : <Code embedded />}
    </section>
  )
}

import { useEffect, useState } from 'react'
import { Check } from 'lucide-react'
import type { Job } from '../../api'
import { cn } from '@/lib/utils'
import { STEP_LABEL, stepsOf } from './sources'

// m:ss since a time, ticking every second while shown.
function useElapsed(since: string | null): string {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  if (!since) return '0:00'
  const secs = Math.max(0, Math.floor((now - new Date(since).getTime()) / 1000))
  return `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`
}

// A repository sync as the worker reports it: the stages done, the one running, those to come,
// a bar for how far along the stages it is and the time it has taken.
export default function SyncProgress({ job }: { job: Job }) {
  const { steps, current } = stepsOf(job)
  const queued = job.status === 'queued'
  const elapsed = useElapsed(queued ? job.created_at : job.started_at)
  const share = ((current + 0.5) / steps.length) * 100
  const detail = job.progress?.detail
  const found =
    !queued && detail?.markdown !== undefined
      ? [
          detail.code && `${detail.code === 'typescript' ? 'TypeScript' : 'JavaScript'} code`,
          `${detail.markdown} Markdown files`,
        ]
          .filter(Boolean)
          .join(' · ')
      : null

  return (
    <div className="mt-3 flex flex-col gap-2" data-testid="sync-progress">
      <ol aria-label="Sync stages" className="m-0 flex list-none flex-wrap items-center gap-1 p-0">
        {steps.map((step, i) => {
          const state = i < current ? 'done' : i === current ? 'current' : 'next'
          return (
            <li
              key={step}
              aria-current={state === 'current' ? 'step' : undefined}
              className="flex items-center gap-1 text-[12px]"
            >
              {i > 0 && (
                <span
                  aria-hidden
                  className={cn(
                    'h-px w-3 transition-colors duration-500 sm:w-5',
                    i <= current ? 'bg-info' : 'bg-border',
                  )}
                />
              )}
              <span
                className={cn(
                  'flex items-center gap-1.5 rounded-full px-2 py-0.5 transition-colors duration-500',
                  state === 'done' && 'text-muted-foreground',
                  state === 'current' && 'bg-info-bg font-medium text-info',
                  state === 'next' && 'text-muted-foreground',
                )}
              >
                {state === 'done' ? (
                  <Check aria-hidden className="size-3 text-success" />
                ) : state === 'current' ? (
                  <span aria-hidden className="relative flex size-2">
                    <span className="absolute inline-flex size-full animate-ping rounded-full bg-info opacity-60 motion-reduce:animate-none" />
                    <span className="relative inline-flex size-2 rounded-full bg-info" />
                  </span>
                ) : (
                  <span
                    aria-hidden
                    className="size-2 rounded-full border border-muted-foreground/50"
                  />
                )}
                {STEP_LABEL[step]}
                <span className="sr-only">
                  {state === 'done' ? ' (done)' : state === 'current' ? ' (in progress)' : ''}
                </span>
              </span>
            </li>
          )
        })}
      </ol>
      <div
        role="progressbar"
        aria-label="Sync progress"
        aria-valuemin={0}
        aria-valuemax={steps.length}
        aria-valuenow={current}
        aria-valuetext={`${STEP_LABEL[steps[current]]}, step ${current + 1} of ${steps.length}`}
        className="relative h-1 overflow-hidden rounded-full bg-secondary"
      >
        {queued ? (
          <div className="absolute inset-y-0 w-2/5 animate-indeterminate rounded-full bg-info/70 motion-reduce:animate-none" />
        ) : (
          <div
            className="h-full rounded-full bg-info transition-[width] duration-700 ease-out"
            style={{ width: `${share}%` }}
          />
        )}
      </div>
      <p className="m-0 flex flex-wrap gap-x-2 text-[12px] text-muted-foreground tabular-nums">
        <span>{queued ? 'Waiting for the worker' : `${STEP_LABEL[steps[current]]}…`}</span>
        {found && <span>· found {found}</span>}
        <span className="ml-auto">{elapsed}</span>
      </p>
    </div>
  )
}

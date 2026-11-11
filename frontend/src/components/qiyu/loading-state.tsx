import { useSyncExternalStore } from 'react'
import { useDelayedBusy } from '@/hooks/use-delayed-busy'
import { requestActivity } from '@/lib/request-activity'
import { cn } from '@/lib/utils'
import { RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'

export function LoadingMark({ className }: { className?: string }) {
  return <span aria-hidden="true" className={cn('qiyu-loading-mark size-4 shrink-0', className)} />
}

export function LoadingValue({ className }: { className?: string }) {
  return <span className={cn('qiyu-skeleton inline-block h-8 w-20 rounded-md align-middle', className)}><span className="sr-only">正在读取</span></span>
}

export function LoadingState({ label = '正在读取数据…', rows = 3, className }: { label?: string; rows?: number; className?: string }) {
  return <div role="status" aria-live="polite" className={cn('flex min-h-44 w-full flex-col justify-center gap-5 py-6 text-left', className)}>
    <div className="flex items-center gap-2 text-sm text-muted-foreground"><LoadingMark />{label}</div>
    <div aria-hidden="true" className="qiyu-skeleton-pulse space-y-4">{Array.from({ length: rows }, (_, index) => <div key={index} className="flex items-center gap-5">
      <span className={cn('qiyu-skeleton h-3.5 rounded-sm', index % 2 ? 'w-2/5' : 'w-3/5')} />
      <span className="qiyu-skeleton ml-auto h-3.5 w-1/5 rounded-sm" />
    </div>)}</div>
  </div>
}

export function PageActivity() {
  const label = useSyncExternalStore(requestActivity.subscribe, requestActivity.getForegroundLabel)
  const visible = useDelayedBusy(label !== null)
  return <div role="status" aria-live="polite" data-visible={visible} className="qiyu-request-progress pointer-events-none absolute inset-x-0 bottom-0 z-20 h-0.5 overflow-hidden">
    <span className="sr-only">{visible && label !== null ? label || '正在加载…' : ''}</span>
    <span aria-hidden="true" className="qiyu-request-progress-line block h-full w-1/3" />
  </div>
}

export function RefreshDataButton({ onRefresh }: { onRefresh: () => void }) {
  const busy = useSyncExternalStore(requestActivity.subscribe, requestActivity.getBusy)
  const spinning = useDelayedBusy(busy)
  return <Button variant="ghost" size="icon-sm" onClick={onRefresh} aria-label="刷新数据" aria-busy={busy}><RefreshCw className={cn('size-3.5', spinning && 'animate-spin')} /></Button>
}

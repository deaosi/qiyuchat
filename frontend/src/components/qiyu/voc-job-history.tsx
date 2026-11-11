import type { ReactElement } from 'react'
import { RefreshCw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { useApi } from '@/hooks/use-api'
import { LoadingMark, LoadingState } from './loading-state'

const statusLabels = {
  queued: '排队中',
  running: '整理中',
  completed: '已完成',
  partial: '部分完成',
  failed: '失败',
  skipped: '已跳过',
} as const

type Job = {
  id: string
  status: keyof typeof statusLabels
  total: number
  processed: number
  failed: number
  skipped: number
  error: string
  created_at?: string
  updated_at?: string
}

function JobRecord({ job }: { job: Job }) {
  const active = job.status === 'queued' || job.status === 'running'
  const counts = [
    { label: '计划', value: job.total },
    { label: '已处理', value: job.processed },
    { label: '失败', value: job.failed },
    { label: '跳过', value: job.skipped },
  ]

  return <li className="space-y-3 rounded-lg border p-3.5 sm:p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <Badge variant={job.status === 'failed' ? 'destructive' : job.status === 'partial' ? 'secondary' : 'outline'}>
        {active && <LoadingMark className="size-3" />}{statusLabels[job.status]}
      </Badge>
      <span className="text-xs tabular-nums text-muted-foreground">提交 <time>{job.created_at || '—'}</time></span>
    </div>
    <dl className="grid grid-cols-4 gap-2 text-sm">
      {counts.map(({ label, value }) => <div key={label} className="min-w-0">
        <dt className="text-xs text-muted-foreground">{label}</dt>
        <dd className="mt-1 font-medium tabular-nums">{value.toLocaleString('zh-CN')}</dd>
      </div>)}
    </dl>
    <p className="text-xs tabular-nums text-muted-foreground">{active ? '最近更新' : '结束'} <time>{job.updated_at || '—'}</time></p>
    {job.error && <p className="whitespace-pre-wrap break-words border-t pt-2 text-xs leading-5 text-muted-foreground">{job.error}</p>}
  </li>
}

function JobHistoryContent() {
  const jobs = useApi<{ items: Job[] }>('voc2/jobs', { source: 'local' })
  const items = jobs.data?.items ?? []
  const busy = jobs.loading || jobs.refreshing

  return <>
      <div className="min-h-0 space-y-3 overflow-y-auto px-0.5" aria-busy={busy}>
        {jobs.error && <div role="alert" className="rounded-lg border bg-muted/25 px-4 py-3 text-sm">
          <p className="font-medium">读取记录失败</p>
          <p className="mt-1 break-words text-muted-foreground">{jobs.error}</p>
        </div>}
        {jobs.loading && <LoadingState label="正在读取模型整理记录…" rows={2} />}
        {items.length > 0 && <ol aria-label="模型整理任务" className="space-y-2.5">{items.map(job => <JobRecord key={job.id} job={job} />)}</ol>}
        {!jobs.loading && !jobs.error && items.length === 0 && <p role="status" className="py-10 text-center text-sm text-muted-foreground">暂无模型整理记录</p>}
      </div>
      <DialogFooter className="border-t pt-3">
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={jobs.retry}>
          <RefreshCw aria-hidden="true" />{jobs.error ? '重试读取' : '刷新记录'}
        </Button>
        <DialogClose asChild><Button type="button" size="sm">关闭</Button></DialogClose>
      </DialogFooter>
  </>
}

export function VocJobHistory({ open, onOpenChange, trigger }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  trigger?: ReactElement
}) {
  return <Dialog open={open} onOpenChange={onOpenChange}>
    {trigger && <DialogTrigger asChild>{trigger}</DialogTrigger>}
    <DialogContent showCloseButton={false} className="grid max-h-[calc(100dvh-2rem)] min-w-0 grid-rows-[auto_minmax(0,1fr)_auto] sm:max-w-xl">
      <DialogHeader>
        <DialogTitle>模型整理记录</DialogTitle>
        <DialogDescription>最近 30 次任务，按提交时间倒序。时间为北京时间。</DialogDescription>
      </DialogHeader>
      <JobHistoryContent />
    </DialogContent>
  </Dialog>
}

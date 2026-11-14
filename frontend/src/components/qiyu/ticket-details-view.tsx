import { Badge } from '@/components/ui/badge'
import { httpsLink } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { TicketAttachments, ticketTime, type TicketAttachment, type TicketField } from './ticket-form'

export type TicketEvent = {
  id: string
  action: string
  operator: string
  time: number | null
  details: TicketField[]
  images?: TicketAttachment[]
  attachments?: TicketAttachment[]
}

type TicketInfoData = {
  submitter?: string
  created_at?: number | null
  updated_at?: number | null
  category?: string
  template?: string
}

export function TicketInfo({ ticket }: { ticket: TicketInfoData }) {
  const items = [
    ...(ticket.submitter !== undefined ? [['提报人', ticket.submitter]] : []),
    ...(ticket.created_at !== undefined ? [['创建时间', ticketTime(ticket.created_at)]] : []),
    ...(ticket.updated_at !== undefined ? [['更新时间', ticketTime(ticket.updated_at)]] : []),
    ...(ticket.category ? [['工单分类', ticket.category]] : []),
    ...(ticket.template ? [['工单模板', ticket.template]] : []),
  ]
  if (!items.length) return null
  return <dl aria-label="工单信息" className="grid grid-cols-2 gap-x-3 gap-y-4 rounded-lg border bg-muted/25 p-4 text-sm sm:grid-cols-3">
    {items.map(([name, value]) => <div key={name} className="min-w-0">
      <dt className="mb-1 text-xs text-muted-foreground">{name}</dt>
      <dd className="tabular-nums [overflow-wrap:anywhere]">{value || '—'}</dd>
    </div>)}
  </dl>
}

export function TicketHistory({ history, available, reload }: { history: TicketEvent[]; available: boolean; reload?: () => void }) {
  if (!available) return <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 text-sm text-destructive">
    <p>暂时无法读取七鱼工单处理记录。</p>
    {reload && <Button size="sm" variant="outline" onClick={reload}>重试</Button>}
  </div>
  if (!history.length) return <p className="text-sm text-muted-foreground">暂无工单处理记录。</p>
  return <ol className="divide-y rounded-lg border">
    {history.map((event, index) => <li key={`${event.id}-${index}`} className="space-y-2 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className="break-words text-sm font-medium">{event.action || '工单操作'}</p>
        <p className="shrink-0 text-xs tabular-nums text-muted-foreground">{ticketTime(event.time)}</p>
      </div>
      {event.operator && <p className="text-xs text-muted-foreground">{event.operator}</p>}
      {event.details.length > 0 && <dl className="space-y-2 text-sm leading-6">
        {event.details.map((field, i) => <div key={`${field.label}-${i}`}>
          {field.label && <dt className="text-xs text-muted-foreground">{field.label}</dt>}
          <dd className="space-y-2 [overflow-wrap:anywhere]">
            {field.value && <p className="whitespace-pre-wrap">{field.value}</p>}
            {(!!field.images?.length || !!field.attachments?.length) &&
              <TicketAttachments images={field.images} attachments={field.attachments} />}
          </dd>
        </div>)}
      </dl>}
      {(!!event.images?.length || !!event.attachments?.length) &&
        <TicketAttachments images={event.images} attachments={event.attachments} />}
    </li>)}
  </ol>
}

export type TechnicalFeedback = {
  status: string
  reply: string
  resolved_version: string
  received_at: number | null
  writeback_status: 'pending' | 'succeeded' | 'failed' | 'uncertain' | ''
  written_at: number | null
  error?: string
  feishu_url?: string
}

const writebackLabels: Record<TechnicalFeedback['writeback_status'], string> = {
  '': '回传状态未知',
  pending: '待回传七鱼',
  succeeded: '已回传七鱼',
  failed: '回传七鱼失败',
  uncertain: '待核对七鱼回复',
}

export function TicketTechnicalFeedback({ feedback }: { feedback: TechnicalFeedback }) {
  const feishuUrl = httpsLink(feedback.feishu_url)
  return <section aria-label="飞书技术处理" className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">飞书技术处理</h3>
        <Badge variant={feedback.writeback_status === 'failed' ? 'destructive' : 'outline'} className="font-normal">
          {writebackLabels[feedback.writeback_status] || '回传状态未知'}
        </Badge>
      </div>
      {feishuUrl && <Button asChild variant="outline" size="sm">
        <a href={feishuUrl} target="_blank" rel="noreferrer">在飞书查看工单</a>
      </Button>}
    </div>
    <div className="space-y-4 rounded-lg border bg-muted/25 p-4">
      <dl className="grid grid-cols-2 gap-3 text-sm">
        {[
          ['技术处理状态', feedback.status],
          ['解决版本', feedback.resolved_version],
          ['飞书变更接收时间', ticketTime(feedback.received_at)],
          ['回传成功时间', ticketTime(feedback.written_at)],
        ].map(([label, value]) => <div key={label} className="min-w-0">
          <dt className="mb-1 text-xs text-muted-foreground">{label}</dt>
          <dd className="tabular-nums [overflow-wrap:anywhere]">{value || '—'}</dd>
        </div>)}
        {feedback.reply && <div className="col-span-full min-w-0">
          <dt className="mb-1 text-xs text-muted-foreground">技术回复</dt>
          <dd className="whitespace-pre-wrap rounded-md border bg-background p-3 text-sm leading-relaxed [overflow-wrap:anywhere]">{feedback.reply}</dd>
        </div>}
      </dl>
      {feedback.error && <p className="text-sm text-muted-foreground [overflow-wrap:anywhere]">{feedback.error}</p>}
    </div>
  </section>
}

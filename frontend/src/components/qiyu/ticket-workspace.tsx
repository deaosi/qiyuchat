import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { ChevronLeft, ChevronRight, Search } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useApi } from '@/hooks/use-api'
import { normalizePair } from '@/lib/api'
import { LoadingState, RefreshDataButton } from './loading-state'
import { QADetail } from './qa-detail'
import { TicketImagePreview } from './ticket-image-preview'

const pageSize = 20
const numberFormat = new Intl.NumberFormat('zh-CN')
const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

type Ticket = {
  id: string
  title: string
  status: string
  priority: string
  owner: string
  created_at: number | null
  updated_at: number | null
  linked_session_count: number
}
type TicketPage = { items: Ticket[]; total: number; offset: number; limit: number; has_more: boolean }
type TicketAttachment = { url: string; name: string }
type TicketField = { label: string; value: string; images?: TicketAttachment[]; attachments?: TicketAttachment[] }
type TicketEvent = {
  id: string
  action: string
  operator: string
  time: number | null
  details: TicketField[]
  images?: TicketAttachment[]
  attachments?: TicketAttachment[]
}
type LinkedSession = { session_id: string; started_at: number | null; qa_id: number | null }
type TicketDetail = {
  ticket: Ticket & { description: string; category?: string; template?: string; fields?: TicketField[]; images?: TicketAttachment[]; attachments?: TicketAttachment[] }
  history: TicketEvent[]
  sessions: LinkedSession[]
  history_available: boolean
}
const statuses = { all: '全部状态', '1': '已提交', '5': '待申领', '10': '处理中', '20': '已完结' } as const
type TicketStatus = keyof typeof statuses
type Filters = { q: string; start: string; end: string; ticket_status: TicketStatus }
const emptyFilters: Filters = { q: '', start: '', end: '', ticket_status: 'all' }

function ticketTime(value: number | null): string {
  if (typeof value !== 'number' || value <= 0 || !Number.isFinite(new Date(value).getTime())) return '—'
  return timeFormat.format(value).replaceAll('/', '-')
}

function TicketError({ error, retry }: { error: string; retry: () => void }) {
  return <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 text-sm text-destructive">
    <p>{error}</p><Button size="sm" variant="outline" onClick={retry}>重试</Button>
  </div>
}

function TicketAttachments({ images = [], attachments = [] }: { images?: TicketAttachment[]; attachments?: TicketAttachment[] }) {
  return <div className="space-y-3">
    {attachments.length > 0 && <ul className="space-y-2">{attachments.map(file => <li key={file.url}>
      <Button asChild variant="outline" size="sm" className="h-auto max-w-full whitespace-normal py-1.5 text-left [overflow-wrap:anywhere]">
        <a href={file.url} download>{file.name || '下载附件'}</a>
      </Button>
    </li>)}</ul>}
    {images.map(image => <TicketImagePreview key={image.url} {...image} />)}
  </div>
}

function TicketFormRow({ label, children }: { label: string; children: ReactNode }) {
  return <div className="grid min-w-0 gap-1.5 sm:grid-cols-[9rem_minmax(0,1fr)] sm:items-start sm:gap-5">
    <dt className="text-sm font-medium leading-6 sm:pt-1 sm:text-right">{label}</dt>
    <dd className="min-w-0 rounded-md border bg-background px-3 py-1.5 text-sm leading-6 [overflow-wrap:anywhere]">{children}</dd>
  </div>
}

function TicketForm({ ticket }: { ticket: TicketDetail['ticket'] }) {
  const fields = ticket.fields ?? []
  const priority = fields.find(field => field.label === '优先级')
  const request = fields.find(field => field.label === '问题需求')
  const attachmentField = fields.find(field => /附件|截图/.test(field.label))
  const fieldUrls = new Set(fields.flatMap(field => [...(field.images ?? []), ...(field.attachments ?? [])].map(file => file.url)))
  const images = (ticket.images ?? []).filter(file => !fieldUrls.has(file.url))
  const attachments = (ticket.attachments ?? []).filter(file => !fieldUrls.has(file.url))
  const hasMedia = images.length > 0 || attachments.length > 0
  return <dl aria-label="工单提报" className="space-y-3">
    <TicketFormRow label="优先级">{priority?.value || ticket.priority || '—'}</TicketFormRow>
    {fields.filter(field => field.label !== '优先级').map((field, index) => {
      const fieldImages = field === attachmentField ? [...(field.images ?? []), ...images] : field.images
      const fieldAttachments = field === attachmentField ? [...(field.attachments ?? []), ...attachments] : field.attachments
      return <TicketFormRow label={field.label} key={`${field.label}-${index}`}>
        {field.value && <p className="whitespace-pre-wrap">{field.value}</p>}
        {fieldImages?.length || fieldAttachments?.length ?
          <div className={field.value ? 'mt-2' : ''}><TicketAttachments images={fieldImages} attachments={fieldAttachments} /></div> :
          !field.value && <span className="text-muted-foreground">{field === attachmentField ? '暂无附件。' : '—'}</span>}
      </TicketFormRow>
    })}
    {(!request || (ticket.description && ticket.description !== request.value)) && <TicketFormRow label={request ? '工单描述' : '问题需求'}>
      <p className="min-h-12 whitespace-pre-wrap">{ticket.description || '—'}</p>
    </TicketFormRow>}
    {!attachmentField && (hasMedia || !fields.some(field => field.images?.length || field.attachments?.length)) &&
      <TicketFormRow label="附件／截图">{hasMedia ? <TicketAttachments images={images} attachments={attachments} /> :
        <span className="text-muted-foreground">暂无附件。</span>}</TicketFormRow>}
  </dl>
}

function TicketHistory({ history, available, reload }: { history: TicketEvent[]; available: boolean; reload: () => void }) {
  if (!available) return <TicketError error="暂时无法读取七鱼工单处理记录。" retry={reload} />
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

function TicketDetails({ id, refreshKey, onClose, onCloseAutoFocus }: {
  id: string
  refreshKey: number
  onClose: () => void
  onCloseAutoFocus: (event: Event) => void
}) {
  const details = useApi<TicketDetail>('tickets/detail', { source: 'local', id }, refreshKey)
  const [session, setSession] = useState<LinkedSession | null>(null)
  const dialogTitle = useRef<HTMLHeadingElement>(null)
  const data = details.data
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}>
    <DialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl"
      onOpenAutoFocus={event => {
        event.preventDefault()
        dialogTitle.current?.focus({ preventScroll: true })
      }}
      onCloseAutoFocus={onCloseAutoFocus}>
      <DialogHeader className="shrink-0 border-b px-5 py-5 pr-12 text-left sm:px-6 sm:pr-12">
        <DialogTitle ref={dialogTitle} tabIndex={-1} className="text-lg leading-6 outline-none [overflow-wrap:anywhere]">{data?.ticket.title || '工单详情'}</DialogTitle>
        <DialogDescription>七鱼工单 #{id}{data?.ticket.status ? ` · ${data.ticket.status}` : ''}</DialogDescription>
      </DialogHeader>
      <div className="min-h-0 flex-1 overflow-auto px-5 py-5 sm:px-6">
        {details.loading ? <LoadingState label="正在读取七鱼工单…" /> : <div className="space-y-6">
          {details.error && <TicketError error={details.error} retry={details.retry} />}
          {data && <>
            <section className="space-y-4">
              <h3 className="text-sm font-semibold">工单提报</h3>
              <TicketForm ticket={data.ticket} />
            </section>
            <dl aria-label="工单信息" className="grid grid-cols-2 gap-x-3 gap-y-4 rounded-lg border bg-muted/25 p-4 text-sm sm:grid-cols-3">
              {[
                ['受理人', data.ticket.owner],
                ['创建时间', ticketTime(data.ticket.created_at)], ['更新时间', ticketTime(data.ticket.updated_at)],
                ...(data.ticket.category ? [['工单分类', data.ticket.category]] : []),
                ...(data.ticket.template ? [['工单模板', data.ticket.template]] : []),
              ].map(([name, value]) => <div key={name} className="min-w-0">
                <dt className="mb-1 text-xs text-muted-foreground">{name}</dt>
                <dd className="tabular-nums [overflow-wrap:anywhere]">{value || '—'}</dd>
              </div>)}
            </dl>
            <section aria-label="处理记录" className="space-y-2">
              <h3 className="text-sm font-semibold">处理记录</h3>
              <TicketHistory history={data.history} available={data.history_available} reload={details.retry} />
            </section>
            <section aria-label="关联会话" className="space-y-2">
              <h3 className="text-sm font-semibold">关联会话</h3>
              {data.sessions.length ? <ul className="divide-y rounded-lg border">{data.sessions.map(item => <li key={item.session_id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm">
                <span className="break-all">{item.session_id}<span className="ml-2 text-xs text-muted-foreground">{ticketTime(item.started_at)}</span></span>
                {item.qa_id !== null && <Button variant="outline" size="sm" aria-expanded={session?.session_id === item.session_id}
                  onClick={() => setSession(current => current?.session_id === item.session_id ? null : item)}>
                  {session?.session_id === item.session_id ? '收起会话' : '查看会话'}
                </Button>}
              </li>)}</ul> : <p className="text-sm text-muted-foreground">语海暂未记录与该工单关联的会话。</p>}
              {session && <div className="rounded-lg border p-4">
                <QADetail pair={normalizePair({ qa_id: session.qa_id, session_id: session.session_id, asked_at: session.started_at })}
                  sessionContext embedded onClose={() => setSession(null)} />
              </div>}
            </section>
          </>}
        </div>}
      </div>
    </DialogContent>
  </Dialog>
}

export function TicketWorkspace({ selected, onSelect, refreshKey = 0 }: {
  selected: string | null
  onSelect: (id: string | null) => void
  refreshKey?: number
}) {
  const [draft, setDraft] = useState<Filters>(emptyFilters)
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [offset, setOffset] = useState(0)
  const [validation, setValidation] = useState('')
  const opener = useRef<HTMLButtonElement>(null)
  const title = useRef<HTMLHeadingElement>(null)
  const query = useApi<TicketPage>('tickets', { source: 'local', ...filters, ticket_status: filters.ticket_status === 'all' ? undefined : filters.ticket_status, offset, limit: pageSize }, refreshKey)
  const data = query.data
  const total = data?.total ?? 0
  const finalOffset = Math.max(0, Math.ceil(total / pageSize) - 1) * pageSize
  const correctingPage = data !== null && !data.has_more && !data.items.length && offset > finalOffset
  useEffect(() => {
    if (correctingPage) setOffset(finalOffset)
  }, [correctingPage, finalOffset])

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (draft.start && draft.end && draft.start > draft.end) {
      setValidation('开始日期不能晚于结束日期。')
      return
    }
    setValidation('')
    setFilters({ ...draft, q: draft.q.trim() })
    setOffset(0)
  }

  function reset() {
    setDraft(emptyFilters)
    setFilters(emptyFilters)
    setValidation('')
    setOffset(0)
  }

  return <Card className="min-w-0">
    <CardHeader className="gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <CardTitle><h2 ref={title} tabIndex={-1}>七鱼工单</h2></CardTitle>
        <RefreshDataButton onRefresh={query.retry} />
      </div>
      <form role="search" aria-label="筛选工单" className="flex flex-wrap items-end gap-3" onSubmit={search}>
        <label className="grid min-w-40 flex-1 gap-1.5 text-xs text-muted-foreground sm:max-w-72">工单搜索
          <Input type="search" maxLength={100} value={draft.q} placeholder="工单编号或标题" onChange={event => setDraft(current => ({ ...current, q: event.target.value }))} />
        </label>
        <label className="grid gap-1.5 text-xs text-muted-foreground">创建日期从
          <Input type="date" min="2000-01-01" max="2100-12-31" value={draft.start} onChange={event => setDraft(current => ({ ...current, start: event.target.value }))} />
        </label>
        <label className="grid gap-1.5 text-xs text-muted-foreground">创建日期至
          <Input type="date" min="2000-01-01" max="2100-12-31" value={draft.end} onChange={event => setDraft(current => ({ ...current, end: event.target.value }))} />
        </label>
        <div className="grid gap-1.5 text-xs text-muted-foreground">
          <span id="ticket-status-label">状态</span>
          <Select value={draft.ticket_status} onValueChange={value => { if (Object.hasOwn(statuses, value)) setDraft(current => ({ ...current, ticket_status: value as TicketStatus })) }}>
            <SelectTrigger className="w-28" aria-labelledby="ticket-status-label"><SelectValue /></SelectTrigger>
            <SelectContent>{Object.entries(statuses).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <Button type="submit" variant="outline"><Search />查询</Button>
        <Button type="button" variant="ghost" onClick={reset}>重置</Button>
      </form>
      {validation && <p className="text-sm text-destructive" role="alert">{validation}</p>}
      {(!filters.start && !filters.end) && <p className="text-xs text-muted-foreground">默认显示最近 30 天创建的工单。</p>}
    </CardHeader>
    <CardContent className="min-w-0 space-y-3">
      {query.error && <TicketError error={query.error} retry={query.retry} />}
      {query.loading || correctingPage ? <LoadingState label="正在读取七鱼工单列表…" /> : data && (data.items.length ?
        <div className="overflow-hidden rounded-lg border">
          <Table className="xl:min-w-[780px]">
            <TableHeader className="bg-muted/45"><TableRow className="[&_th]:px-2 sm:[&_th]:px-4">
              <TableHead className="w-full xl:w-[34%]">工单</TableHead><TableHead>状态</TableHead>
              <TableHead className="hidden xl:table-cell">优先级</TableHead>
              <TableHead className="hidden xl:table-cell">受理人</TableHead>
              <TableHead className="hidden xl:table-cell">创建时间</TableHead>
              <TableHead className="hidden xl:table-cell">更新时间</TableHead>
              <TableHead><span className="sr-only">操作</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>{data.items.map(item => <TableRow key={item.id} className="[&_td]:px-2 [&_td]:py-3 sm:[&_td]:px-4">
              <TableCell className="whitespace-normal"><p className="font-medium [overflow-wrap:anywhere]">{item.title || '未填写标题'}</p><p className="mt-1 text-xs tabular-nums text-muted-foreground">#{item.id}{item.linked_session_count > 0 && ` · ${numberFormat.format(item.linked_session_count)} 个关联会话`}</p></TableCell>
              <TableCell>{item.status ? <Badge variant="outline" className="font-normal">{item.status}</Badge> : '—'}</TableCell>
              <TableCell className="hidden xl:table-cell">{item.priority || '—'}</TableCell>
              <TableCell className="hidden whitespace-normal xl:table-cell">{item.owner || '—'}</TableCell>
              <TableCell className="hidden text-xs tabular-nums text-muted-foreground xl:table-cell">{ticketTime(item.created_at)}</TableCell>
              <TableCell className="hidden text-xs tabular-nums text-muted-foreground xl:table-cell">{ticketTime(item.updated_at)}</TableCell>
              <TableCell className="text-right"><Button type="button" variant="outline" size="sm" aria-label={`查看工单 ${item.id}`} onClick={event => {
                opener.current = event.currentTarget
                onSelect(item.id)
              }}>查看详情</Button></TableCell>
            </TableRow>)}</TableBody>
          </Table>
        </div> : <p role="status" className="rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">当前筛选没有匹配的工单。</p>)}
      {data && !correctingPage && total > 0 && <nav aria-label="工单分页" className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
        <span aria-live="polite">第 {numberFormat.format(data.offset + 1)}–{numberFormat.format(data.offset + data.items.length)} 条，共 {numberFormat.format(total)} 条</span>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" disabled={offset === 0 || query.refreshing} onClick={() => setOffset(current => Math.max(0, current - pageSize))}><ChevronLeft />上一页</Button>
          <Button size="sm" variant="outline" disabled={!data.has_more || query.refreshing} onClick={() => setOffset(current => current + pageSize)}>下一页<ChevronRight /></Button>
        </div>
      </nav>}
      {selected && <TicketDetails key={selected} id={selected} refreshKey={refreshKey} onClose={() => onSelect(null)} onCloseAutoFocus={event => {
        const focusTarget = opener.current?.isConnected ? opener.current : title.current
        if (focusTarget) { event.preventDefault(); focusTarget.focus({ preventScroll: true }) }
        opener.current = null
      }} />}
    </CardContent>
  </Card>
}

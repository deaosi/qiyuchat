import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { ChevronLeft, ChevronRight, Search } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useApi } from '@/hooks/use-api'
import { ApiError, apiPost, normalizePair } from '@/lib/api'
import { LoadingState, RefreshDataButton } from './loading-state'
import { QADetail } from './qa-detail'
import { TicketHistory, TicketInfo, TicketTechnicalFeedback, type TechnicalFeedback, type TicketEvent } from './ticket-details-view'
import { TicketForm, ticketTime, type TicketAttachment, type TicketField } from './ticket-form'

const pageSize = 20
const numberFormat = new Intl.NumberFormat('zh-CN')
type Ticket = {
  id: string
  title: string
  status: string
  priority: string
  submitter: string
  created_at: number | null
  updated_at: number | null
  linked_session_count: number
}
type TicketPage = { items: Ticket[]; total: number; offset: number; limit: number; has_more: boolean }
type LinkedSession = { session_id: string; started_at: number | null; qa_id: number | null }
type TicketEdit = {
  revision: string
  title: string
  priority: number
  fields: { id: string; label: string; value: string; multiline: boolean; required?: boolean }[]
}
type TicketUpdate = { revision: string; title?: string; priority?: number; fields?: Record<string, string> }
type TicketDetail = {
  ticket: Ticket & { description: string; category?: string; template?: string; fields?: TicketField[]; images?: TicketAttachment[]; attachments?: TicketAttachment[] }
  history: TicketEvent[]
  sessions: LinkedSession[]
  history_available: boolean
  technical_feedback?: TechnicalFeedback | null
  edit?: TicketEdit
}
const statuses = { all: '全部状态', '1': '已提交', '5': '待申领', '10': '处理中', '20': '已完结' } as const
const priorities = { 5: '一般', 8: '紧急', 10: '非常紧急' } as const
const editTextarea = 'min-h-24 w-full rounded-md border bg-background px-3 py-2 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:opacity-50'
type TicketStatus = keyof typeof statuses
type Filters = { q: string; start: string; end: string; ticket_status: TicketStatus }
const emptyFilters: Filters = { q: '', start: '', end: '', ticket_status: 'all' }

function TicketError({ error, retry }: { error: string; retry: () => void }) {
  return <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 text-sm text-destructive">
    <p>{error}</p><Button size="sm" variant="outline" onClick={retry}>重试</Button>
  </div>
}

function TicketEditor({ edit, saving, failure, conflict, onSave, onCancel, onReload }: {
  edit: TicketEdit
  saving: boolean
  failure: string
  conflict: boolean
  onSave: (update: TicketUpdate) => void
  onCancel: () => void
  onReload: () => void
}) {
  const fieldPrefix = useId()
  const titleInput = useRef<HTMLInputElement>(null)
  const [title, setTitle] = useState(edit.title)
  const [priority, setPriority] = useState(String(edit.priority))
  const [fields, setFields] = useState<Record<string, string>>(() => Object.fromEntries(edit.fields.map(field => [field.id, field.value])))
  const changedTitle = title.trim() !== edit.title.trim()
  const changedPriority = priority !== String(edit.priority)
  const changedFields = edit.fields.filter(field => fields[field.id] !== field.value)
  const dirty = changedTitle || changedPriority || changedFields.length > 0
  useEffect(() => { titleInput.current?.focus({ preventScroll: true }) }, [])

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving || conflict || !dirty || !title.trim() || changedFields.some(field => field.required && !fields[field.id].trim())) return
    onSave({
      revision: edit.revision,
      ...(changedTitle ? { title: title.trim() } : {}),
      ...(changedPriority ? { priority: Number(priority) } : {}),
      ...(changedFields.length ? { fields: Object.fromEntries(changedFields.map(field => [field.id, fields[field.id].trim()])) } : {}),
    })
  }

  return <form aria-label="编辑工单" aria-busy={saving} className="space-y-4" onSubmit={submit}>
    <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_10rem]">
      <div className="space-y-2">
        <Label htmlFor={`${fieldPrefix}-title`}>工单标题</Label>
        <Input ref={titleInput} id={`${fieldPrefix}-title`} value={title} maxLength={100} required
          disabled={saving} onChange={event => setTitle(event.target.value)} />
      </div>
      <div className="space-y-2">
        <Label htmlFor={`${fieldPrefix}-priority`}>优先级</Label>
        <Select value={priority} disabled={saving} onValueChange={setPriority}>
          <SelectTrigger id={`${fieldPrefix}-priority`} className="w-full"><SelectValue /></SelectTrigger>
          <SelectContent>
            {!Object.hasOwn(priorities, edit.priority) && <SelectItem value={String(edit.priority)}>当前优先级</SelectItem>}
            {Object.entries(priorities).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
    </div>
    {edit.fields.map((field, index) => {
      const inputId = `${fieldPrefix}-field-${index}`
      const props = { id: inputId, disabled: saving, required: field.required && fields[field.id] !== field.value, value: fields[field.id], onChange: (event: { target: { value: string } }) => {
        const value = event.target.value
        setFields(current => ({ ...current, [field.id]: value }))
      } }
      return <div className="space-y-2" key={field.id}>
        <Label htmlFor={inputId}>{field.label}</Label>
        {field.multiline ? <textarea {...props} className={editTextarea} rows={4} maxLength={4000} /> : <Input {...props} maxLength={300} />}
      </div>
    })}
    {failure && <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 text-sm text-destructive">
      <p>{failure}</p>
      {conflict && <Button type="button" size="sm" variant="outline" onClick={onReload}>读取最新工单</Button>}
    </div>}
    <div className="flex flex-wrap justify-end gap-2">
      <Button type="button" variant="outline" disabled={saving} onClick={onCancel}>取消</Button>
      <Button type="submit" disabled={saving || conflict || !dirty || !title.trim()}>{saving ? '正在保存…' : '保存修改'}</Button>
    </div>
  </form>
}

function TicketDetails({ id, refreshKey, onClose, onCloseAutoFocus, onUpdated }: {
  id: string
  refreshKey: number
  onClose: () => void
  onCloseAutoFocus: (event: Event) => void
  onUpdated: () => void
}) {
  const details = useApi<TicketDetail>('tickets/detail', { source: 'local', id }, refreshKey)
  const [session, setSession] = useState<LinkedSession | null>(null)
  const [edit, setEdit] = useState<TicketEdit | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [conflict, setConflict] = useState(false)
  const [saved, setSaved] = useState<{ previous: TicketDetail | null; detail: TicketDetail } | null>(null)
  const saveRequest = useRef<AbortController | null>(null)
  const latestDetail = useRef(details.data)
  latestDetail.current = details.data
  const dialogTitle = useRef<HTMLHeadingElement>(null)
  const data = saved?.previous === details.data ? saved.detail : details.data
  useEffect(() => () => saveRequest.current?.abort(), [])

  function cancelEdit() {
    setEdit(null)
    dialogTitle.current?.focus({ preventScroll: true })
  }

  function closeDetails() {
    if (saveRequest.current) return
    if (edit) cancelEdit()
    else onClose()
  }

  async function save(update: TicketUpdate) {
    if (saveRequest.current) return
    const controller = new AbortController()
    saveRequest.current = controller
    setSaving(true)
    setSaveError('')
    try {
      const detail = await apiPost<TicketDetail>('tickets/update', { source: 'local' }, JSON.stringify({ id, ...update }),
        { 'Content-Type': 'application/json' }, controller.signal)
      if (controller.signal.aborted) return
      setSaved({ previous: latestDetail.current, detail })
      cancelEdit()
      onUpdated()
    } catch (error) {
      if (controller.signal.aborted || error instanceof ApiError && error.status === 401) return
      setSaveError(error instanceof Error ? error.message : '暂时无法保存工单。')
      setConflict(error instanceof ApiError && error.status === 409)
    } finally {
      if (saveRequest.current === controller) saveRequest.current = null
      if (!controller.signal.aborted) setSaving(false)
    }
  }

  return <Dialog open onOpenChange={open => { if (!open) closeDetails() }}>
    <DialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl"
      showCloseButton={!saving} onPointerDownOutside={event => { if (edit) event.preventDefault() }}
      onOpenAutoFocus={event => {
        event.preventDefault()
        dialogTitle.current?.focus({ preventScroll: true })
      }}
      onCloseAutoFocus={onCloseAutoFocus}>
      <DialogHeader className="shrink-0 border-b px-5 py-5 pr-12 text-left sm:px-6 sm:pr-12">
        <div className="flex items-start justify-between gap-3">
          <DialogTitle ref={dialogTitle} tabIndex={-1} className="min-w-0 text-lg leading-6 outline-none [overflow-wrap:anywhere]">{data?.ticket.title || '工单详情'}</DialogTitle>
          {data?.edit && !edit && <Button type="button" variant="outline" size="sm" className="shrink-0" disabled={details.refreshing} onClick={() => {
            setConflict(false)
            setSaveError('')
            setEdit(data.edit ?? null)
          }}>编辑工单</Button>}
        </div>
        <DialogDescription>七鱼工单 #{id}{data?.ticket.status ? ` · ${data.ticket.status}` : ''}</DialogDescription>
      </DialogHeader>
      <div className="min-h-0 flex-1 overflow-auto px-5 py-5 sm:px-6">
        {details.loading ? <LoadingState label="正在读取七鱼工单…" /> : <div className="space-y-6">
          {details.error && <TicketError error={details.error} retry={details.retry} />}
          {data && <>
            <section className="space-y-4">
              <h3 className="text-sm font-semibold">{edit ? '编辑提报内容' : '工单提报'}</h3>
              {edit ? <TicketEditor edit={edit} saving={saving} failure={saveError} conflict={conflict}
                onSave={save} onCancel={cancelEdit} onReload={() => { cancelEdit(); details.retry() }} /> : <TicketForm ticket={data.ticket} />}
            </section>
            <TicketInfo ticket={data.ticket} />
            {data.technical_feedback && <TicketTechnicalFeedback feedback={data.technical_feedback} />}
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
              <TableHead className="hidden xl:table-cell">提报人</TableHead>
              <TableHead className="hidden xl:table-cell">创建时间</TableHead>
              <TableHead className="hidden xl:table-cell">更新时间</TableHead>
              <TableHead><span className="sr-only">操作</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>{data.items.map(item => <TableRow key={item.id} className="[&_td]:px-2 [&_td]:py-3 sm:[&_td]:px-4">
              <TableCell className="whitespace-normal"><p className="font-medium [overflow-wrap:anywhere]">{item.title || '未填写标题'}</p><p className="mt-1 text-xs tabular-nums text-muted-foreground">#{item.id}{item.linked_session_count > 0 && ` · ${numberFormat.format(item.linked_session_count)} 个关联会话`}</p></TableCell>
              <TableCell>{item.status ? <Badge variant="outline" className="font-normal">{item.status}</Badge> : '—'}</TableCell>
              <TableCell className="hidden xl:table-cell">{item.priority || '—'}</TableCell>
                <TableCell className="hidden whitespace-normal xl:table-cell">{item.submitter || '—'}</TableCell>
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
      {selected && <TicketDetails key={selected} id={selected} refreshKey={refreshKey} onClose={() => onSelect(null)} onUpdated={query.retry} onCloseAutoFocus={event => {
        const focusTarget = opener.current?.isConnected ? opener.current : title.current
        if (focusTarget) { event.preventDefault(); focusTarget.focus({ preventScroll: true }) }
        opener.current = null
      }} />}
    </CardContent>
  </Card>
}

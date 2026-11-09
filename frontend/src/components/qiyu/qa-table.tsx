import * as React from 'react'
import {
  ArrowUpRight, Check, ChevronDown, ChevronLeft, ChevronRight,
  ChevronsLeft, ChevronsRight, CircleCheck, CircleDashed,
  AlertCircle, Columns3, Loader2, RefreshCw, Search, SearchX, X,
} from 'lucide-react'

import type { QAPair } from '@/lib/demo-data'
import { ApiError, apiGet, normalizePair, type ApiFilters } from '@/lib/api'
import { cn, customerLabel } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'

type ReplyTab = 'all' | 'answered' | 'unanswered'
type ColumnId = 'question' | 'answer' | 'customer' | 'status' | 'time'

const columnOptions: { id: ColumnId; label: string }[] = [
  { id: 'question', label: '客户问题' }, { id: 'answer', label: '客服回复' },
  { id: 'customer', label: '客户与客户群' }, { id: 'status', label: '回复状态' },
  { id: 'time', label: '提问时间' },
]

export interface QATableProps {
  data: QAPair[]
  onSelect: (pair: QAPair) => void
  initialReply?: ReplyTab
  remote?: { filters: ApiFilters; refreshKey?: number }
  onFiltersChange?: (filters: ApiFilters) => void
}

interface PairsResponse {
  items: Record<string, unknown>[]
  total: number
  offset: number
  limit: number
}

function pairTime(value: string) {
  return /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(value)
    ? { date: value.slice(5, 10).replace('-', '/'), time: value.slice(11, 16) }
    : { date: '时间未知', time: '' }
}

export function QATable({ data, onSelect, initialReply, remote, onFiltersChange }: QATableProps) {
  const initialTab: ReplyTab = initialReply ?? (remote?.filters.replied === 'answered' || remote?.filters.replied === 'unanswered' ? remote.filters.replied : 'all')
  const [query, setQuery] = React.useState('')
  const [debouncedQuery, setDebouncedQuery] = React.useState('')
  const [reply, setReply] = React.useState<ReplyTab>(initialTab)
  const [page, setPage] = React.useState(0)
  const [pageSize, setPageSize] = React.useState(8)
  const [selected, setSelected] = React.useState<Set<string>>(() => new Set())
  const [visible, setVisible] = React.useState<Set<ColumnId>>(
    () => new Set(columnOptions.map(column => column.id)),
  )
  const [retry, setRetry] = React.useState(0)
  const [server, setServer] = React.useState<{
    key: string; rows: QAPair[]; total: number | null; loading: boolean; error: string | null
  }>({ key: '', rows: [], total: null, loading: Boolean(remote), error: null })
  const requestSequence = React.useRef(0)
  const onFiltersChangeRef = React.useRef(onFiltersChange)
  const isRemote = Boolean(remote)
  const filtersKey = remote ? JSON.stringify(remote.filters) : ''
  const refreshKey = remote?.refreshKey

  React.useEffect(() => { setReply(initialTab); setPage(0) }, [initialTab])
  React.useEffect(() => {
    if (!filtersKey) return
    const filters = JSON.parse(filtersKey) as ApiFilters
    setPage(0)
    setQuery(filters.q || '')
    setDebouncedQuery(filters.q || '')
    setSelected(new Set())
  }, [filtersKey])
  React.useEffect(() => { setSelected(new Set()) }, [refreshKey])
  React.useEffect(() => { onFiltersChangeRef.current = onFiltersChange }, [onFiltersChange])
  const exportFilterKey = filtersKey ? JSON.stringify({
    ...JSON.parse(filtersKey), q: query.trim(), replied: reply === 'all' ? '' : reply,
  }) : ''
  React.useEffect(() => {
    if (!exportFilterKey) return
    const filters = JSON.parse(exportFilterKey) as ApiFilters
    delete filters.limit
    delete filters.offset
    onFiltersChangeRef.current?.(filters)
  }, [exportFilterKey])
  React.useEffect(() => {
    if (!isRemote) return
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 280)
    return () => window.clearTimeout(timer)
  }, [query, isRemote])
  const requestKey = filtersKey ? JSON.stringify({
    ...JSON.parse(filtersKey),
    q: debouncedQuery,
    replied: reply === 'all' ? '' : reply,
    limit: pageSize,
    offset: page * pageSize,
  }) : ''
  React.useEffect(() => {
    if (!requestKey) return
    const controller = new AbortController()
    const sequence = ++requestSequence.current
    setServer({ key: requestKey, rows: [], total: null, loading: true, error: null })
    apiGet<PairsResponse>('pairs', JSON.parse(requestKey) as ApiFilters, controller.signal)
      .then(result => {
        if (controller.signal.aborted || sequence !== requestSequence.current) return
        const total = Math.max(0, Number(result.total) || 0)
        const lastPage = Math.max(0, Math.ceil(total / pageSize) - 1)
        if (page > lastPage) { setPage(lastPage); return }
        setServer({ key: requestKey, rows: (result.items || []).map(normalizePair), total, loading: false, error: null })
      })
      .catch(error => {
        if (controller.signal.aborted || sequence !== requestSequence.current) return
        if (error instanceof ApiError && error.status === 401) return
        setServer({ key: requestKey, rows: [], total: null, loading: false, error: error instanceof Error ? error.message : '读取问答失败，请重试。' })
      })
    return () => controller.abort()
  }, [requestKey, refreshKey, retry, page, pageSize])
  const keywordRows = React.useMemo(() => {
    const value = query.trim().toLocaleLowerCase()
    return value ? data.filter(row => [row.question, row.answer, row.customerName,
      row.customerId, row.groupName, row.staffName, row.sessionId, row.id]
      .some(field => field.toLocaleLowerCase().includes(value))) : data
  }, [data, query])
  const answered = keywordRows.filter(row => Boolean(row.answer.trim())).length
  const unanswered = keywordRows.length - answered
  const filtered = React.useMemo(() => keywordRows.filter(row =>
    reply === 'all' || (reply === 'answered' ? Boolean(row.answer.trim()) : !row.answer.trim()),
  ), [keywordRows, reply])
  const total = isRemote ? server.total ?? 0 : filtered.length
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const currentPage = isRemote ? page : Math.min(page, pages - 1)
  const pageRows = isRemote ? server.rows : filtered.slice(currentPage * pageSize, (currentPage + 1) * pageSize)
  const selectedCount = isRemote ? selected.size : filtered.filter(row => selected.has(row.id)).length
  const selectedOnPage = pageRows.filter(row => selected.has(row.id)).length
  const pending = isRemote && (server.key !== requestKey || server.loading || query.trim() !== debouncedQuery)
  const failed = isRemote && server.key === requestKey && server.error
  function changeQuery(value: string) { setQuery(value); setPage(0); setSelected(new Set()) }
  function changeReply(value: ReplyTab) { setReply(value); setPage(0); setSelected(new Set()) }
  function clearTableFilters() { changeQuery(''); changeReply('all') }

  function selectPage(value: boolean) {
    setSelected(current => {
      const next = new Set(current)
      pageRows.forEach(row => value ? next.add(row.id) : next.delete(row.id))
      return next
    })
  }
  function selectRow(id: string, value: boolean) {
    setSelected(current => {
      const next = new Set(current)
      value ? next.add(id) : next.delete(id)
      return next
    })
  }
  function showColumn(id: ColumnId, value: boolean) {
    if (id === 'question') return
    setVisible(current => {
      const next = new Set(current)
      value ? next.add(id) : next.delete(id)
      return next
    })
  }

  return (
    <section className="flex min-w-0 flex-col gap-4" aria-label="问答明细">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs value={reply} onValueChange={value => changeReply(value as ReplyTab)}>
          <TabsList>
            <TabsTrigger value="all">全部问答 {!isRemote && <Badge variant="secondary" className="ml-1.5 rounded-sm px-1.5 text-[11px]">{keywordRows.length}</Badge>}</TabsTrigger>
            <TabsTrigger value="answered">有回复 {!isRemote && <Badge variant="secondary" className="ml-1.5 rounded-sm px-1.5 text-[11px]">{answered}</Badge>}</TabsTrigger>
            <TabsTrigger value="unanswered">未发现回复 {!isRemote && <Badge variant="secondary" className="ml-1.5 rounded-sm px-1.5 text-[11px]">{unanswered}</Badge>}</TabsTrigger>
          </TabsList>
        </Tabs>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm"><Columns3 />显示列<ChevronDown className="ml-1" /></Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuLabel>选择显示的字段</DropdownMenuLabel>
            <DropdownMenuSeparator />
            {columnOptions.map(column => (
              <DropdownMenuCheckboxItem key={column.id} checked={visible.has(column.id)}
                disabled={column.id === 'question'} onSelect={event => event.preventDefault()}
                onCheckedChange={checked => showColumn(column.id, Boolean(checked))}>
                {column.label}
              </DropdownMenuCheckboxItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full max-w-md">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={query} onChange={event => changeQuery(event.target.value)} maxLength={200}
            placeholder="搜索问题、回复、客户名称或会话 ID…" aria-label="搜索问答"
            className="h-9 pl-9 pr-9" />
          {query && <Button variant="ghost" size="icon-xs" className="absolute right-1.5 top-1/2 -translate-y-1/2"
            onClick={() => changeQuery('')} aria-label="清空搜索"><X /></Button>}
        </div>
        <p className="text-xs text-muted-foreground">{pending ? '正在读取问答…' : failed ? '问答读取失败' : `共 ${total} 条匹配记录 · 点击问题追溯上下文`}</p>
      </div>

      <div className="overflow-hidden rounded-lg border">
        <Table className="min-w-[850px]">
          <TableHeader className="bg-muted/50">
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-10 pl-4">
                <Checkbox aria-label="选择当前页全部问答" disabled={pending || Boolean(failed) || !pageRows.length}
                  checked={Boolean(pageRows.length) && selectedOnPage === pageRows.length ? true : selectedOnPage ? 'indeterminate' : false}
                  onCheckedChange={checked => selectPage(Boolean(checked))} />
              </TableHead>
              {visible.has('question') && <TableHead className="min-w-64">客户问题</TableHead>}
              {visible.has('answer') && <TableHead className="min-w-56">客服回复</TableHead>}
              {visible.has('customer') && <TableHead className="min-w-36">客户 / 客户群</TableHead>}
              {visible.has('status') && <TableHead className="min-w-36">回复状态</TableHead>}
              {visible.has('time') && <TableHead className="min-w-28">提问时间</TableHead>}
              <TableHead className="w-12"><span className="sr-only">查看详情</span></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {pending ? <TableRow><TableCell colSpan={visible.size + 2} className="h-60 text-center"><div className="flex flex-col items-center gap-3" role="status"><Loader2 className="size-5 animate-spin text-muted-foreground" /><span className="text-sm text-muted-foreground">正在读取问答记录…</span></div></TableCell></TableRow> : failed ? <TableRow><TableCell colSpan={visible.size + 2} className="h-60 text-center"><div className="mx-auto flex max-w-md flex-col items-center gap-3" role="alert"><AlertCircle className="size-6 text-muted-foreground" /><div className="font-medium">暂时无法读取问答</div><p className="text-sm text-muted-foreground">{server.error}</p><Button variant="outline" size="sm" onClick={() => setRetry(value => value + 1)}><RefreshCw className="size-3.5" />重新加载</Button></div></TableCell></TableRow> : pageRows.length ? pageRows.map(row => (
              <TableRow key={row.id} data-state={selected.has(row.id) ? 'selected' : undefined}>
                <TableCell className="pl-4 align-top pt-5">
                  <Checkbox checked={selected.has(row.id)} onCheckedChange={checked => selectRow(row.id, Boolean(checked))}
                    aria-label={`选择问答 ${row.id}`} />
                </TableCell>
                {visible.has('question') && <TableCell className="py-4 align-top">
                  <button className="line-clamp-2 max-w-[340px] text-left font-medium leading-6 hover:underline underline-offset-4"
                    onClick={() => onSelect(row)}>{row.question}</button>
                  <div className="mt-1.5 flex items-center gap-2 text-xs text-muted-foreground">
                    <span>{row.id}</span><span className="text-border">/</span><span>{row.clusterId ? `问题簇 ${row.clusterId}` : '未归类'}</span>
                  </div>
                </TableCell>}
                {visible.has('answer') && <TableCell className="py-4 align-top">
                  <div className={cn('line-clamp-2 max-w-[310px] leading-6 text-muted-foreground', !row.answer.trim() && 'text-muted-foreground/65')}>
                    {row.answer.trim() ? row.answer : '当前抽取片段中未发现客服回复'}
                  </div>
                </TableCell>}
                {visible.has('customer') && <TableCell className="py-4 align-top">
                  <span className="font-medium">{customerLabel(row.customerName, row.customerId)}</span>
                  <div className="mt-1.5 text-xs text-muted-foreground">{row.groupName}</div>
                </TableCell>}
                {visible.has('status') && <TableCell className="py-4 align-top">
                  <Badge variant="outline" className="gap-1.5 whitespace-nowrap px-2 text-xs font-normal">
                    {row.answer.trim() ? <CircleCheck className="size-3.5 fill-foreground text-background" /> : <CircleDashed className="size-3.5 text-muted-foreground" />}
                    {row.answer.trim() ? '有客服回复' : '未发现回复'}
                  </Badge>
                  <div className="mt-1.5 text-xs text-muted-foreground">接待客服 · {row.staffName}</div>
                </TableCell>}
                {visible.has('time') && <TableCell className="py-4 align-top tabular-nums">
                  {pairTime(row.askedAt).date}
                  {pairTime(row.askedAt).time && <div className="mt-1.5 text-xs text-muted-foreground">{pairTime(row.askedAt).time}</div>}
                </TableCell>}
                <TableCell className="pr-3 align-top pt-4">
                  <Button variant="ghost" size="icon-sm" onClick={() => onSelect(row)} aria-label={`查看 ${row.id} 的会话详情`}>
                    <ArrowUpRight className="size-4 text-muted-foreground" />
                  </Button>
                </TableCell>
              </TableRow>
            )) : (
              <TableRow><TableCell colSpan={visible.size + 2} className="h-60 text-center">
                <div className="flex flex-col items-center gap-3">
                  <SearchX className="size-7 text-muted-foreground/60" />
                  <div className="font-medium">没有匹配的问答</div>
                  <p className="text-sm text-muted-foreground">试试其他关键词、回复状态，或调整上方全局筛选。</p>
                  <Button variant="outline" size="sm" onClick={clearTableFilters}>清空表格筛选</Button>
                </div>
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-4 px-1">
        <div className="text-sm text-muted-foreground">
          {selectedCount ? <span className="inline-flex items-center gap-1.5"><Check className="size-3.5" />已选 {selectedCount} 条</span> : <span>{pending ? '正在加载…' : failed ? '读取失败，可重新加载' : `共 ${total} 条问答记录`}</span>}
          {selectedCount > 0 && <button className="ml-3 text-xs underline underline-offset-4" onClick={() => setSelected(new Set())}>取消选择</button>}
        </div>
        <div className="flex flex-wrap items-center gap-5 sm:gap-7">
          <div className="flex items-center gap-2">
            <Label htmlFor="qa-page-size" className="text-sm">每页</Label>
            <Select value={String(pageSize)} onValueChange={value => { setPageSize(Number(value)); setPage(0) }} disabled={pending || Boolean(failed)}>
              <SelectTrigger size="sm" className="w-[72px]" id="qa-page-size"><SelectValue /></SelectTrigger>
              <SelectContent side="top"><SelectItem value="8">8 条</SelectItem><SelectItem value="16">16 条</SelectItem></SelectContent>
            </Select>
          </div>
          <span className="text-sm font-medium tabular-nums">{pending || failed ? '— / —' : `第 ${currentPage + 1} / ${pages} 页`}</span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="icon-sm" className="hidden sm:inline-flex" disabled={pending || Boolean(failed) || currentPage === 0} onClick={() => setPage(0)} aria-label="首页"><ChevronsLeft /></Button>
            <Button variant="outline" size="icon-sm" disabled={pending || Boolean(failed) || currentPage === 0} onClick={() => setPage(currentPage - 1)} aria-label="上一页"><ChevronLeft /></Button>
            <Button variant="outline" size="icon-sm" disabled={pending || Boolean(failed) || currentPage >= pages - 1} onClick={() => setPage(currentPage + 1)} aria-label="下一页"><ChevronRight /></Button>
            <Button variant="outline" size="icon-sm" className="hidden sm:inline-flex" disabled={pending || Boolean(failed) || currentPage >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="末页"><ChevronsRight /></Button>
          </div>
        </div>
      </div>
    </section>
  )
}

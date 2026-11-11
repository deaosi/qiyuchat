import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useApi } from '@/hooks/use-api'
import { customerLabel } from '@/lib/utils'
import type { ApiFilters } from '@/lib/api'
import { LoadingState, LoadingValue } from './loading-state'

const pageSize = 20
const numberFormat = new Intl.NumberFormat('zh-CN')
const textLink = 'h-auto max-w-full justify-start whitespace-normal p-0 text-left leading-5 text-foreground [overflow-wrap:anywhere]'

type Sort = 'questions' | 'unanswered' | 'recent'
type Reply = 'all' | 'unanswered'
type Question = { cluster_id: number; question: string; count: number }
type AudienceItem = {
  group_name: string
  total_questions: number
  unanswered: number
  last_seen?: string
  top: Question[]
}
type Group = AudienceItem & { customer_count: number }
type Customer = AudienceItem & {
  customer_id: string
  customer_name: string
  group_names: string[]
  group_count: number
}
type Directory<T> = {
  items: T[]
  total: number
  offset: number
  limit: number
  totals: { questions: number; unanswered: number }
}
type Props = {
  kind: 'groups' | 'customers'
  filters: ApiFilters
  refreshKey?: number
  scopedGroup?: string
  onClearScopedGroup: () => void
  onViewCustomers: (group: string) => void
  onDrill: (next: Partial<ApiFilters>, reply?: Reply, customerName?: string) => void
}

function Unanswered({ count, name, onSelect }: { count: number; name: string; onSelect: () => void }) {
  if (count === 0) return <span className="text-muted-foreground">0</span>
  return <Button variant="link" className={`${textLink} underline decoration-muted-foreground/45 hover:decoration-foreground`} aria-label={`${name}，未发现回复 ${count} 条`} onClick={onSelect}>{numberFormat.format(count)}</Button>
}

function IssueLinks({ questions, onSelect }: { questions: Question[]; onSelect: (cluster: number) => void }) {
  if (!questions.length) return <span className="text-muted-foreground">—</span>
  return <div className="space-y-1">
    {questions.map(question => <div key={question.cluster_id} className="flex items-baseline justify-between gap-2">
      <Button variant="link" className={`${textLink} min-w-0 shrink text-xs font-normal`} title={question.question} onClick={() => onSelect(question.cluster_id)}>
        <span className="line-clamp-2">{question.question}</span>
      </Button>
      <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{numberFormat.format(question.count)} 次</span>
    </div>)}
  </div>
}

function GroupRows({ items, onDrill, onViewCustomers }: { items: Group[]; onDrill: Props['onDrill']; onViewCustomers: Props['onViewCustomers'] }) {
  return items.map(item => {
    const scope = { group: item.group_name }
    return <TableRow key={item.group_name} className="[&_td]:px-4 [&_td]:py-3">
      <TableCell className="whitespace-normal"><Button variant="link" className={textLink} onClick={() => onDrill(scope)}>{item.group_name}</Button></TableCell>
      <TableCell className="tabular-nums">{numberFormat.format(item.total_questions)}</TableCell>
      <TableCell className="tabular-nums">{numberFormat.format(item.customer_count)}</TableCell>
      <TableCell className="tabular-nums"><Unanswered count={item.unanswered} name={item.group_name} onSelect={() => onDrill(scope, 'unanswered')} /></TableCell>
      <TableCell className="whitespace-normal text-xs leading-5 text-muted-foreground">{item.last_seen || '—'}</TableCell>
      <TableCell className="whitespace-normal"><IssueLinks questions={item.top.slice(0, 3)} onSelect={cluster => onDrill({ ...scope, cluster })} /></TableCell>
      <TableCell className="text-right"><Button variant="ghost" size="sm" aria-label={`查看客户：${item.group_name}`} onClick={() => onViewCustomers(item.group_name)}>查看客户</Button></TableCell>
    </TableRow>
  })
}

function CustomerRows({ items, group, onDrill }: { items: Customer[]; group?: string; onDrill: Props['onDrill'] }) {
  return items.map(item => {
    const scope = { customer: item.customer_id, ...(group ? { group } : {}) }
    const name = customerLabel(item.customer_name, item.customer_id)
    const memberships = item.group_names ?? (item.group_name ? [item.group_name] : [])
    const membershipLabel = memberships.join('、')
    const extra = Math.max(0, (item.group_count ?? memberships.length) - memberships.length)
    return <TableRow key={item.customer_id} className="[&_td]:px-4 [&_td]:py-3">
      <TableCell className="whitespace-normal"><Button variant="link" className={textLink} onClick={() => onDrill(scope, 'all', item.customer_name)}>{name}</Button></TableCell>
      <TableCell className="whitespace-normal text-xs leading-5 text-muted-foreground">
        <span className="line-clamp-2 [overflow-wrap:anywhere]" title={membershipLabel || undefined}>{membershipLabel || '—'}</span>
        {extra > 0 && <span className="block">另有 {numberFormat.format(extra)} 个群</span>}
      </TableCell>
      <TableCell className="tabular-nums">{numberFormat.format(item.total_questions)}</TableCell>
      <TableCell className="tabular-nums"><Unanswered count={item.unanswered} name={name} onSelect={() => onDrill(scope, 'unanswered', item.customer_name)} /></TableCell>
      <TableCell className="whitespace-normal text-xs leading-5 text-muted-foreground">{item.last_seen || '—'}</TableCell>
      <TableCell className="whitespace-normal"><IssueLinks questions={item.top?.slice(0, 2) ?? []} onSelect={cluster => onDrill({ ...scope, cluster }, 'all', item.customer_name)} /></TableCell>
    </TableRow>
  })
}

export function AudienceWorkspace({ kind, filters, refreshKey = 0, scopedGroup, onClearScopedGroup, onViewCustomers, onDrill }: Props) {
  const isGroups = kind === 'groups'
  const label = isGroups ? '客户群' : '客户'
  const { source, days, start, end, staff } = filters
  const group = isGroups ? filters.group : scopedGroup || filters.group
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<Sort>('questions')
  const results = useRef<HTMLHeadingElement>(null)
  const scope = JSON.stringify([kind, source, days, start, end, group, staff, query, sort])
  const [cursor, setCursor] = useState({ scope, offset: 0 })
  const offset = cursor.scope === scope ? cursor.offset : 0
  const request = { source, days, start, end, group, staff, entity_q: query || undefined, entity_sort: sort, limit: pageSize, offset, top: isGroups ? 3 : undefined }
  const { data, loading, error, retry } = useApi<Directory<Group | Customer>>(kind, request, refreshKey)
  const total = data?.total ?? data?.items.length ?? 0
  const lastPageOffset = Math.max(0, Math.ceil(total / pageSize) - 1) * pageSize
  const correctingPage = data !== null && offset > lastPageOffset

  useEffect(() => {
    setCursor(current => current.scope === scope ? current : { scope, offset: 0 })
  }, [scope])
  useEffect(() => {
    if (correctingPage) setCursor({ scope, offset: lastPageOffset })
  }, [correctingPage, lastPageOffset, scope])

  function changePage(next: number) {
    setCursor({ scope, offset: next })
    results.current?.focus({ preventScroll: true })
    results.current?.scrollIntoView?.({ block: 'start' })
  }

  const metrics = [
    { label: isGroups ? '客户群数' : '客户数', value: data?.total },
    { label: '有效问答', value: data?.totals?.questions },
    { label: '未发现回复', value: data?.totals?.unanswered },
  ]
  const items = data?.items ?? []

  return <div className="space-y-4">
    <dl className="grid grid-cols-3 overflow-hidden rounded-xl border bg-card shadow-xs">
      {metrics.map(metric => <div key={metric.label} className="min-w-0 border-r bg-linear-to-b from-background to-muted/25 px-3 py-4 last:border-r-0 sm:px-5">
        <dt className="text-xs text-muted-foreground sm:text-sm">{metric.label}</dt>
        <dd className="mt-1 text-xl font-semibold tabular-nums sm:text-2xl">{metric.value !== undefined ? numberFormat.format(metric.value) : loading ? <LoadingValue className="h-6 w-12 sm:w-16" /> : '—'}</dd>
      </div>)}
    </dl>

    <Card className="min-w-0">
      <CardHeader className="gap-4">
        <CardTitle><h2 ref={results} className="scroll-mt-4" tabIndex={-1}>{isGroups ? '客户群咨询分布' : '客户咨询记录'}</h2></CardTitle>
        {!isGroups && scopedGroup && <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span className="text-muted-foreground">客户群</span>
          <span className="max-w-full font-medium [overflow-wrap:anywhere]">{scopedGroup}</span>
          <Button variant="ghost" size="xs" onClick={onClearScopedGroup}>清除群限定</Button>
        </div>}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <form className="flex w-full flex-wrap items-center gap-2 sm:w-auto" role="search" onSubmit={event => {
            event.preventDefault()
            const next = draft.trim()
            setDraft(next)
            setQuery(next)
            setCursor({ scope, offset: 0 })
          }}>
            <Input className="min-w-24 flex-1 sm:w-64 sm:flex-none" type="search" aria-label={`搜索${label}`} placeholder={isGroups ? '搜索群名称' : '搜索名称、ID 或客户群'} value={draft} onChange={event => setDraft(event.target.value)} />
            <Button variant="outline" type="submit">搜索</Button>
            {query && <Button variant="ghost" type="button" onClick={() => { setDraft(''); setQuery('') }}>清除搜索</Button>}
          </form>
          <Select value={sort} onValueChange={value => setSort(value as Sort)}>
            <SelectTrigger aria-label="排序方式"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="questions">按问答量排序</SelectItem>
              <SelectItem value="unanswered">按未发现回复排序</SelectItem>
              <SelectItem value="recent">按最近咨询排序</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </CardHeader>
      <CardContent className="min-w-0 space-y-3">
        {error && <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 text-sm text-destructive">
          {error}<Button size="sm" variant="outline" onClick={retry}>重试</Button>
        </div>}
        {loading || correctingPage ? <LoadingState label={`正在读取${label}…`} /> : data && (items.length ?
          <div className="overflow-hidden rounded-lg border">
            <Table className="table-fixed min-w-[840px]">
              <TableHeader className="bg-muted/45">
                <TableRow className="[&_th]:px-4">
                  <TableHead className="w-[21%]">{label}</TableHead>
                  {!isGroups && <TableHead className="w-[22%]">客户群</TableHead>}
                  <TableHead className="w-22">有效问答</TableHead>
                  {isGroups && <TableHead className="w-20">客户数</TableHead>}
                  <TableHead className="w-27">未发现回复</TableHead>
                  <TableHead className="w-32">最近咨询</TableHead>
                  <TableHead>{isGroups ? '高频问题' : '常问问题'}</TableHead>
                  {isGroups && <TableHead className="w-28 text-right"><span className="sr-only">操作</span></TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {isGroups ? <GroupRows items={items as Group[]} onDrill={onDrill} onViewCustomers={onViewCustomers} /> : <CustomerRows items={items as Customer[]} group={group} onDrill={onDrill} />}
              </TableBody>
            </Table>
          </div> : <p className="rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">{query ? `没有匹配的${label}` : `当前范围暂无${label}记录`}</p>)}
        <nav aria-label={`${label}分页`} className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
          <span aria-live="polite">{data && total > 0 && items.length > 0 ? `第 ${numberFormat.format(offset + 1)}–${numberFormat.format(offset + items.length)} 条，共 ${numberFormat.format(total)} 条` : '每页 20 条'}</span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={loading || correctingPage || offset === 0} onClick={() => changePage(offset - pageSize)}><ChevronLeft />上一页</Button>
            <Button variant="outline" size="sm" disabled={loading || correctingPage || offset >= lastPageOffset} onClick={() => changePage(offset + pageSize)}>下一页<ChevronRight /></Button>
          </div>
        </nav>
        {items.length > 0 && <p className="text-xs text-muted-foreground">“未发现回复”表示会话记录中未提取到客服回复。</p>}
      </CardContent>
    </Card>
  </div>
}

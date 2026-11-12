import { Fragment, useEffect, useId, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useApi } from '@/hooks/use-api'
import type { ApiFilters, ClusterItem } from '@/lib/api'
import { LoadingState, LoadingValue } from './loading-state'

const pageSize = 20
const numbers = new Intl.NumberFormat('zh-CN')
const rowLink = 'h-auto p-0 text-xs text-foreground underline decoration-muted-foreground/40 hover:decoration-foreground'

type Sort = NonNullable<ApiFilters['entity_sort']>
type Members = 'all' | 'repeated' | 'single'
type Reply = 'all' | 'unanswered' | 'answered'
type Page = { items: ClusterItem[]; total: number; offset: number; limit: number; totals: { questions: number; unanswered: number } }
type Props = {
  filters: ApiFilters
  refreshKey?: number
  onDrill: (next: Partial<ApiFilters>, reply?: Reply) => void
}

export function RuleClusterWorkspace({ filters, refreshKey = 0, onDrill }: Props) {
  const { source, days, start, end, group, staff } = filters
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<Sort>('questions')
  const [members, setMembers] = useState<Members>('all')
  const heading = useRef<HTMLHeadingElement>(null)
  const detailsPrefix = useId()
  const scope = JSON.stringify([source, days, start, end, group, staff, query, sort, members])
  const [cursor, setCursor] = useState({ scope, offset: 0, expanded: null as number | null })
  const { offset, expanded } = cursor.scope === scope ? cursor : { offset: 0, expanded: null }
  const request = {
    source, days, start, end, group, staff, entity_q: query || undefined, entity_sort: sort,
    cluster_members: members === 'all' ? undefined : members, limit: pageSize, offset,
  }
  const { data, loading, error, retry } = useApi<Page>('clusters', request, refreshKey)
  const total = data?.total ?? 0
  const items = data?.items ?? []
  const lastOffset = Math.max(0, Math.ceil(total / pageSize) - 1) * pageSize
  const correctingPage = data !== null && offset > lastOffset
  const pending = loading || correctingPage

  useEffect(() => {
    setCursor(current => current.scope === scope ? current : { scope, offset: 0, expanded: null })
  }, [scope])
  useEffect(() => {
    if (correctingPage) setCursor({ scope, offset: lastOffset, expanded: null })
  }, [correctingPage, lastOffset, scope])

  function changePage(next: number) {
    setCursor({ scope, offset: next, expanded: null })
    heading.current?.focus({ preventScroll: true })
    heading.current?.scrollIntoView?.({ block: 'start' })
  }

  const metrics = [
    { label: '规则问题簇', value: data?.total },
    { label: '簇内问答', value: data?.totals.questions },
    { label: '未发现回复', value: data?.totals.unanswered },
  ]

  return <div className="space-y-4">
    <dl className="grid grid-cols-3 overflow-hidden rounded-xl border bg-card shadow-xs">
      {metrics.map(metric => <div key={metric.label} className="min-w-0 border-r bg-linear-to-b from-background to-muted/25 px-3 py-4 last:border-r-0 sm:px-5">
        <dt className="text-xs text-muted-foreground sm:text-sm">{metric.label}</dt>
        <dd className="mt-1 text-xl font-semibold tabular-nums sm:text-2xl">{metric.value !== undefined ? numbers.format(metric.value) : pending ? <LoadingValue className="h-6 w-12 sm:w-16" /> : '—'}</dd>
      </div>)}
    </dl>

    <Card className="min-w-0">
      <CardHeader className="gap-4">
        <div className="space-y-2">
          <CardTitle><h2 ref={heading} className="scroll-mt-4" tabIndex={-1}>相似问法归类</h2></CardTitle>
          <CardDescription>核对规则清洗归并的问法，追溯对应会话。</CardDescription>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <form className="flex w-full flex-wrap items-center gap-2 sm:w-auto" role="search" onSubmit={event => {
            event.preventDefault()
            const next = draft.trim()
            setDraft(next)
            setQuery(next)
            setCursor({ scope, offset: 0, expanded: null })
          }}>
            <Input className="min-w-24 flex-1 sm:w-64 sm:flex-none" type="search" aria-label="搜索规则问题簇" placeholder="搜索问法或编号，如 #12" value={draft} onChange={event => setDraft(event.target.value)} />
            <Button variant="outline" type="submit">搜索</Button>
            {query && <Button variant="ghost" type="button" onClick={() => { setDraft(''); setQuery('') }}>清除搜索</Button>}
          </form>
          <div className="flex flex-wrap gap-2">
            <Select value={members} onValueChange={value => setMembers(value as Members)}>
              <SelectTrigger aria-label="簇内问答数量"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部问题簇</SelectItem>
                <SelectItem value="repeated">多条问答</SelectItem>
                <SelectItem value="single">单条问答</SelectItem>
              </SelectContent>
            </Select>
            <Select value={sort} onValueChange={value => setSort(value as Sort)}>
              <SelectTrigger aria-label="排序方式"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="questions">按问答量排序</SelectItem>
                <SelectItem value="unanswered">按未发现回复排序</SelectItem>
                <SelectItem value="recent">按最近咨询排序</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      </CardHeader>
      <CardContent className="min-w-0 space-y-3">
        {error && <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 text-sm text-destructive">
          {error}<Button size="sm" variant="outline" onClick={retry}>重试</Button>
        </div>}
        {pending ? <LoadingState label="正在读取规则问题簇…" /> : data && (items.length ?
          <div className="overflow-hidden rounded-lg border">
            <Table className="min-w-[690px] table-fixed">
              <TableHeader className="bg-muted/45">
                <TableRow className="[&_th]:px-4">
                  <TableHead>代表问法</TableHead>
                  <TableHead className="w-20">问答量</TableHead>
                  <TableHead className="w-27">未发现回复</TableHead>
                  <TableHead className="w-24">涉及客户</TableHead>
                  <TableHead className="w-21">涉及群</TableHead>
                  <TableHead className="w-28">最近咨询</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map(item => {
                  const id = item.cluster_id
                  const cluster = { cluster: id }
                  const detailsId = `${detailsPrefix}-${id}`
                  const detailsOpen = expanded === id
                  return <Fragment key={id}>
                    <TableRow className="[&_td]:px-4 [&_td]:py-3">
                      <TableCell className="whitespace-normal">
                        <p className="line-clamp-2 font-medium [overflow-wrap:anywhere]" title={item.question}>{item.question}</p>
                        {item.matched_sample && <p className="mt-1 line-clamp-2 text-xs leading-5 [overflow-wrap:anywhere]" title={item.matched_sample}>
                          <span className="mr-2 text-muted-foreground">匹配问法</span>{item.matched_sample}
                        </p>}
                        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                          <span className="tabular-nums">簇 {id} · {numbers.format(item.variants)} 种问法</span>
                          <Button variant="link" className={rowLink} aria-label={`簇 ${id}，查看问答`} onClick={() => onDrill(cluster, 'all')}>查看问答</Button>
                          <Button variant="ghost" size="xs" aria-expanded={detailsOpen} aria-controls={detailsOpen ? detailsId : undefined} aria-label={`簇 ${id}，${detailsOpen ? '收起问法' : '查看问法'}`} onClick={() => setCursor({ scope, offset, expanded: detailsOpen ? null : id })}>
                            {detailsOpen ? '收起问法' : '查看问法'}
                          </Button>
                        </div>
                      </TableCell>
                      <TableCell className="tabular-nums">{numbers.format(item.count)}</TableCell>
                      <TableCell className="tabular-nums">
                        {item.unanswered > 0 ? <Button variant="link" className={`${rowLink} text-sm`} aria-label={`簇 ${id}，未发现回复 ${item.unanswered} 条`} onClick={() => onDrill(cluster, 'unanswered')}>{numbers.format(item.unanswered)}</Button> : <span className="text-muted-foreground">0</span>}
                      </TableCell>
                      <TableCell className="tabular-nums">{numbers.format(item.customers)}</TableCell>
                      <TableCell className="tabular-nums">{numbers.format(item.groups)}</TableCell>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">{item.last_seen || '—'}</TableCell>
                    </TableRow>
                    {detailsOpen && <TableRow id={detailsId} className="bg-muted/25 hover:bg-muted/25">
                      <TableCell colSpan={6} className="px-4 py-3 whitespace-normal">
                        <div className="space-y-2">
                          <p className="text-xs font-medium text-muted-foreground">{item.variants > item.samples.length ? `同簇问法节选 · ${item.samples.length} 种` : '同簇问法'}</p>
                          <ul className="space-y-2">
                            {item.samples.map((sample, index) => <li key={index} className="max-w-2xl text-sm leading-6 [overflow-wrap:anywhere]">{sample}</li>)}
                          </ul>
                        </div>
                      </TableCell>
                    </TableRow>}
                  </Fragment>
                })}
              </TableBody>
            </Table>
          </div> : <p className="rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">{query || members !== 'all' ? '没有符合筛选条件的问题簇' : '当前范围暂无规则问题簇'}</p>)}
        <nav aria-label="规则问题簇分页" className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
          <span aria-live="polite">{!pending && items.length ? `第 ${numbers.format(offset + 1)}–${numbers.format(offset + items.length)} 条，共 ${numbers.format(total)} 条` : '每页 20 条'}</span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={pending || offset === 0} onClick={() => changePage(offset - pageSize)}><ChevronLeft />上一页</Button>
            <Button variant="outline" size="sm" disabled={pending || offset >= lastOffset} onClick={() => changePage(offset + pageSize)}>下一页<ChevronRight /></Button>
          </div>
        </nav>
        {items.length > 0 && <p className="text-xs text-muted-foreground">“未发现回复”表示会话记录中未提取到客服回复。</p>}
      </CardContent>
    </Card>
  </div>
}

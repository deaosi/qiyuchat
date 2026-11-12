import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { ArrowUpRight, ChevronLeft, ChevronRight, Copy, Search } from 'lucide-react'
import { toast } from 'sonner'
import { useApi } from '@/hooks/use-api'
import { useBackgroundRefresh } from '@/hooks/use-background-refresh'
import { normalizePair, type ApiFilters, type VocPlaybook, type VocSop, type VocSopEvidence, type VocSopStep } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { QueryPanel, VocKnowledgeWorkspace, type OpenEvidencePair } from './voc-next-workspace'
import { displayTime } from './voc-topic-evidence'
import { VocPlaybookResults } from './voc-pilot-workspaces'
import { SopDetailHeader, SopDetailSection, SopTimeline, SopTimelineStep } from './voc-detail-display'

type Props = { filters: ApiFilters; refreshKey?: number; onOpenPair: OpenEvidencePair }
const statuses: Record<VocSop['status'], string> = { verified: '效果已验证', guidance: '已核验指引', incomplete: '待补证据' }
const phases: Record<VocSopStep['phase'], string> = { diagnosis: '排查', action: '处理', retest: '复测', escalation: '升级处理' }
const outcomes: Record<VocSopStep['outcome'], string> = { verified: '效果已验证', failed: '尝试未奏效', unconfirmed: '结果待确认' }
const gaps: Record<string, string> = {
  applicability: '适用条件', diagnosis: '排查步骤', action: '处理步骤', retest: '复测步骤',
  customer_result: '客户复测结果', result: '复测结果', unsafe_step: '操作安全依据',
  later_counterevidence: '后续异议或失败的处理结果', unbound_condition: '步骤对应的前置条件',
}
const rank: Record<VocSop['status'], number> = { verified: 0, guidance: 1, incomplete: 2 }
const kinds: Record<VocSop['kind'], string> = { configuration: '配置与使用', anomaly: '异常与排障' }
const pageSize = 10
const count = new Intl.NumberFormat('zh-CN').format
const rowColumns = 'grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 lg:grid-cols-[minmax(0,1fr)_7.5rem_7.5rem_9rem_1rem]'
const statusFilters = ['all', 'verified', 'guidance', 'incomplete'] as const
type StatusFilter = typeof statusFilters[number]

function missingLabels(item: VocSop) {
  return [...new Set(item.missing.map(code => gaps[code] ?? '其他待核验信息'))]
}

function reusableSteps(item: VocSop) {
  return item.steps.filter(step => step.outcome !== 'failed')
}

async function copySop(item: VocSop) {
  const lines = [
    item.title,
    item.problem.quote === item.title ? '' : `问题现象 ${item.problem.quote}`,
    item.conditions.length ? `适用条件 ${item.conditions.map(ref => ref.quote).join('、')}` : '',
    '处理流程',
    ...reusableSteps(item).map((step, index) => `${index + 1}. ${phases[step.phase]} ${step.evidence.quote}`),
    item.status === 'verified' && item.result ? `复测结果 ${item.result.quote}` : '操作效果待验证。',
  ]
  try {
    await navigator.clipboard.writeText(lines.filter(Boolean).join('\n'))
    toast.success('已复制处理 SOP。')
  } catch {
    toast.error('剪贴板暂不可用，可选中文字复制。')
  }
}

function EvidenceLine({ evidence, onOpen }: { evidence: VocSopEvidence; onOpen: (ref: VocSopEvidence) => void }) {
  const time = displayTime(evidence.ts)
  return <div className="space-y-1">
    <p className="whitespace-pre-wrap break-words text-sm leading-6 [overflow-wrap:anywhere]">{evidence.quote}</p>
    <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
      {time && <span className="tabular-nums">{time}</span>}
      <Button type="button" variant="ghost" size="xs" onClick={() => onOpen(evidence)}>定位原话<ArrowUpRight /></Button>
    </div>
  </div>
}

function SopDetail({ item, onOpenPair }: { item: VocSop; onOpenPair: OpenEvidencePair }) {
  const open = (ref: VocSopEvidence) => onOpenPair(normalizePair({
    qa_id: item.qa_id, question: item.problem.quote, asked_at: item.problem.ts,
  }), ref.message_id)
  const missing = missingLabels(item)
  const canCopy = item.status !== 'incomplete' && reusableSteps(item).length > 0
  return <>
    <SopDetailHeader status={statuses[item.status]} verified={item.status === 'verified'} kind={kinds[item.kind]}
      title={<><SheetTitle className="text-lg leading-7 [overflow-wrap:anywhere]">{item.title}</SheetTitle>
        <SheetDescription className="sr-only">查看处理步骤、验证结果和原始会话。</SheetDescription></>}
      metadata={<>
        <span className="tabular-nums">{displayTime(item.problem.ts)}</span>
        <Button type="button" variant="ghost" size="xs" onClick={() => open(item.problem)}>定位问题原话<ArrowUpRight /></Button>
      </>} />
    <div className="min-h-0 flex-1 space-y-6 overflow-y-auto overscroll-contain px-5 py-6 sm:px-7">
      {item.title !== item.problem.quote && <SopDetailSection title="问题现象"><EvidenceLine evidence={item.problem} onOpen={open} /></SopDetailSection>}
      {item.conditions.length > 0 && <SopDetailSection title="适用条件" layout="conditions">
        <div className="space-y-2">{item.conditions.map((ref, index) => <EvidenceLine key={`${ref.message_id}-${index}`} evidence={ref} onOpen={open} />)}</div>
      </SopDetailSection>}
      {item.steps.length > 0 && <SopDetailSection title="处理流程" layout="flow">
        <SopTimeline>
          {item.steps.map((step, index) => <SopTimelineStep key={`${step.evidence.message_id}-${index}`} index={index}
            phase={phases[step.phase]} outcome={outcomes[step.outcome]}
            feedback={step.feedback && step.feedback.message_id !== item.result?.message_id ? <EvidenceLine evidence={step.feedback} onOpen={open} /> : undefined}>
            <EvidenceLine evidence={step.evidence} onOpen={open} />
          </SopTimelineStep>)}
        </SopTimeline>
      </SopDetailSection>}
      {!!item.replies?.length && <SopDetailSection title="人工回复记录">
        <div className="divide-y">{item.replies.map((ref, index) => <div key={`${ref.message_id}-${index}`} className="py-2"><EvidenceLine evidence={ref} onOpen={open} /></div>)}</div>
      </SopDetailSection>}
      {item.result && <SopDetailSection title="复测结果" layout="result"><EvidenceLine evidence={item.result} onOpen={open} /></SopDetailSection>}
      {missing.length > 0 && <SopDetailSection title="待补证据" layout="gaps">
        <p className="text-sm leading-6 text-muted-foreground">{missing.join('、')}</p>
      </SopDetailSection>}
    </div>
    {canCopy && <div className="flex shrink-0 justify-end border-t px-5 py-3 sm:px-7"><Button type="button" size="sm" onClick={() => void copySop(item)}><Copy />复制 SOP</Button></div>}
  </>
}

export function VocSopWorkspace({ filters, refreshKey = 0, onOpenPair }: Props) {
  const [revision, setRevision] = useState(0)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<StatusFilter>('all')
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const rowTrigger = useRef<HTMLButtonElement>(null)
  const searchInput = useRef<HTMLInputElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const scopeKey = JSON.stringify(filters)
  const key = refreshKey + revision
  const sops = useApi<{ items: VocSop[]; total: number }>('voc2/sops', filters, key)
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  useBackgroundRefresh(refresh, false, sops.loading || sops.refreshing, key)
  const items = sops.data?.items ?? []
  const totals = { all: items.length, verified: 0, guidance: 0, incomplete: 0 }
  for (const item of items) totals[item.status]++
  const terms = search.normalize('NFKC').toLowerCase().split(/\s+/).filter(Boolean)
  const matches = items.filter(item => {
    if (status !== 'all' && item.status !== status) return false
    if (!terms.length) return true
    const content = [item.title, item.problem.quote, item.result?.quote, ...item.conditions.map(ref => ref.quote), ...(item.replies?.map(ref => ref.quote) ?? []),
      ...item.steps.flatMap(step => [step.evidence.quote, step.feedback?.quote])].join('\n').normalize('NFKC').toLowerCase()
    return terms.every(term => content.includes(term))
  }).sort((a, b) => rank[a.status] - rank[b.status] || b.problem.ts - a.problem.ts)
  const pageCount = Math.max(1, Math.ceil(matches.length / pageSize))
  const currentPage = Math.min(page, pageCount)
  const start = (currentPage - 1) * pageSize
  const pages = pageCount <= 5 ? Array.from({ length: pageCount }, (_, index) => index + 1)
    : [...new Set([1, currentPage - 1, currentPage, currentPage + 1, pageCount])].filter(value => value >= 1 && value <= pageCount)
  const selected = items.find(item => item.id === selectedId)
  useEffect(() => { setPage(1); setSelectedId(null); setDetailOpen(false) }, [scopeKey])
  useEffect(() => { setPage(value => Math.min(value, pageCount)) }, [pageCount])

  function changePage(next: number) {
    if (currentPage === next) return
    setPage(next)
    heading.current?.focus({ preventScroll: true })
    heading.current?.scrollIntoView?.({ block: 'start' })
  }

  return <>
    <Card className="gap-0 overflow-hidden py-0">
      <div className="flex flex-wrap items-end justify-between gap-4 bg-linear-to-b from-muted/40 to-card px-4 py-5 sm:px-6">
        <div className="space-y-1">
          <h2 ref={heading} tabIndex={-1} className="scroll-mt-4 text-base font-semibold tracking-tight focus:outline-none">问题处理 SOP</h2>
          {sops.data && <p className="text-xs tabular-nums text-muted-foreground">{count(totals.verified + totals.guidance)} 条可复用 · {count(totals.incomplete)} 条待补证据</p>}
        </div>
        <div className="relative w-full sm:w-64">
          <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input ref={searchInput} type="search" value={search} onChange={event => { setSearch(event.target.value); setPage(1) }} maxLength={200}
            placeholder="搜索问题、条件或步骤" aria-label="搜索处理 SOP" className="bg-background pl-9" />
        </div>
      </div>
      <div role="group" aria-label="SOP 状态筛选" className="flex flex-wrap items-center gap-1 border-b px-4 pb-4 sm:px-6">
        {statusFilters.map(value => {
          const label = value === 'all' ? '全部' : statuses[value]
          return <Button key={value} type="button" variant={status === value ? 'default' : 'ghost'} size="sm"
            aria-label={sops.data ? `${label} ${count(totals[value])} 条` : label} aria-pressed={status === value}
            onClick={() => { setStatus(value); setPage(1) }} className="gap-1.5">
            {label}{sops.data && <span className="text-[11px] tabular-nums opacity-70">{count(totals[value])}</span>}
          </Button>
        })}
      </div>
      <QueryPanel hasData={sops.data !== null} loading={sops.loading} error={sops.error} retry={sops.retry}>
        {matches.length ? <>
          <div aria-hidden="true" className={`${rowColumns} hidden border-b bg-muted/25 px-6 py-2.5 text-xs text-muted-foreground lg:grid`}>
            <span>问题</span><span>证据状态</span><span>处理进度</span><span>咨询时间</span><span />
          </div>
          <ul aria-label="SOP 案例" className="divide-y">{matches.slice(start, start + pageSize).map(item => <li key={item.id}>
            <button type="button" aria-haspopup="dialog" aria-label={`${item.title}，${statuses[item.status]}`} onClick={event => { rowTrigger.current = event.currentTarget; setSelectedId(item.id); setDetailOpen(true) }}
              className={`${rowColumns} grid w-full px-4 py-3.5 text-left transition-colors hover:bg-muted/45 focus-visible:bg-muted/50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring sm:px-6`}>
              <span className="min-w-0 space-y-1">
                <span className="line-clamp-2 break-words text-sm font-medium [overflow-wrap:anywhere]" title={item.title}>{item.title}</span>
                <span className="flex flex-wrap gap-x-2 text-xs text-muted-foreground"><span>{kinds[item.kind]}</span>
                  <span className="lg:hidden">{statuses[item.status]} · {item.steps.length ? `${count(item.steps.length)} 个步骤` : '待整理步骤'}</span>
                </span>
              </span>
              <span className="hidden lg:block"><Badge variant={item.status === 'verified' ? 'default' : 'secondary'} className="font-normal">{statuses[item.status]}</Badge></span>
              <span className="hidden text-xs tabular-nums text-muted-foreground lg:block">{item.steps.length ? `${count(item.steps.length)} 个步骤` : '待整理步骤'}</span>
              <span className="hidden text-xs tabular-nums text-muted-foreground lg:block">{displayTime(item.problem.ts)}</span>
              <ChevronRight aria-hidden="true" className="size-4 text-muted-foreground" />
            </button>
          </li>)}</ul>
        </> : <div className="flex min-h-48 flex-col items-center justify-center gap-2 px-6 text-center text-sm text-muted-foreground">
          <p className="font-medium text-foreground">{terms.length ? '未找到匹配的处理 SOP' : status !== 'all' ? `当前范围暂无${statuses[status]}的案例` : '当前范围暂无处理案例'}</p>
          {(terms.length > 0 || status !== 'all') && <Button type="button" variant="ghost" size="sm" onClick={() => { setSearch(''); setStatus('all'); setPage(1) }}>清除筛选</Button>}
        </div>}
      </QueryPanel>
      {matches.length > 0 && <div className="flex flex-wrap items-center justify-between gap-3 border-t bg-muted/15 px-4 py-3 text-xs text-muted-foreground sm:px-6">
        <span aria-live="polite" className="tabular-nums">{count(start + 1)}–{count(Math.min(start + pageSize, matches.length))} / 共 {count(matches.length)} 条</span>
        <nav aria-label="SOP 分页" className="flex flex-wrap items-center gap-1">
          <Button type="button" variant="ghost" size="icon-sm" aria-label="上一页" title="上一页" disabled={currentPage === 1} onClick={() => changePage(currentPage - 1)}><ChevronLeft /></Button>
          {pages.map((number, index) => <Fragment key={number}>
            {index > 0 && number - pages[index - 1] > 1 && <span aria-hidden="true" className="px-1">…</span>}
            <Button type="button" variant={currentPage === number ? 'outline' : 'ghost'} size="icon-sm" aria-label={`第 ${number} 页`}
              aria-current={currentPage === number ? 'page' : undefined} onClick={() => changePage(number)} className="tabular-nums">{number}</Button>
          </Fragment>)}
          <Button type="button" variant="ghost" size="icon-sm" aria-label="下一页" title="下一页" disabled={currentPage === pageCount} onClick={() => changePage(currentPage + 1)}><ChevronRight /></Button>
        </nav>
      </div>}
    </Card>
    <Sheet open={detailOpen && Boolean(selected)} onOpenChange={setDetailOpen}>
      <SheetContent className="w-full gap-0 data-[state=closed]:duration-200 data-[state=open]:duration-250 sm:max-w-2xl" onCloseAutoFocus={event => {
        event.preventDefault()
        if (rowTrigger.current?.isConnected) rowTrigger.current.focus()
        else searchInput.current?.focus()
      }}>
        {selected && <SopDetail item={selected} onOpenPair={onOpenPair} />}
      </SheetContent>
    </Sheet>
  </>
}

export function VocKnowledgeLibrary(props: Props) {
  return <Tabs defaultValue="playbooks" className="gap-4">
    <TabsList aria-label="知识库内容">
      <TabsTrigger value="playbooks">处理手册</TabsTrigger><TabsTrigger value="sop">处理 SOP</TabsTrigger><TabsTrigger value="answers">可复用答案</TabsTrigger>
    </TabsList>
    <TabsContent value="playbooks"><VocPlaybookWorkspace {...props} /></TabsContent>
    <TabsContent value="sop"><VocSopWorkspace {...props} /></TabsContent>
    <TabsContent value="answers"><VocKnowledgeWorkspace {...props} /></TabsContent>
  </Tabs>
}

function VocPlaybookWorkspace({ filters, refreshKey = 0, onOpenPair }: Props) {
  const [revision, setRevision] = useState(0)
  const key = refreshKey + revision
  const playbooks = useApi<{ items: VocPlaybook[]; total: number }>('voc2/playbooks', filters, key)
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  useBackgroundRefresh(refresh, false, playbooks.loading || playbooks.refreshing, key)
  return <Card className="gap-3">
    <CardHeader><CardTitle>问题处理手册</CardTitle><CardDescription>从客户咨询查看问题线索，查阅有会话依据的处理流程。</CardDescription></CardHeader>
    <CardContent><QueryPanel hasData={playbooks.data !== null} loading={playbooks.loading} error={playbooks.error} retry={playbooks.retry}>
      <VocPlaybookResults items={playbooks.data?.items ?? []} onOpenPair={onOpenPair} />
    </QueryPanel></CardContent>
  </Card>
}

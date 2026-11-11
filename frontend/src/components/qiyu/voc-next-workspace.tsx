import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowUpRight, BookOpen, ChevronLeft, ChevronRight, CircleAlert, Copy, LoaderCircle, MessagesSquare, RefreshCw, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import { useApi } from '@/hooks/use-api'
import { useBackgroundRefresh } from '@/hooks/use-background-refresh'
import { apiPost, type ApiFilters, type VocConversationVerification, type VocKnowledgeKind, type VocKnowledgeReuse } from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Progress } from '@/components/ui/progress'
import { LoadingMark, LoadingState, LoadingValue } from './loading-state'
import { diagnosisLabels, displayTime, intentLabels, KnowledgeContent, knowledgeReuseLevel } from './voc-topic-evidence'
import { copyKnowledge, TopicWorkbench, TrendComparison, trendBadgeText, trendLabels, type TrendComparisonData, type TrendFilter, type TopicTrend } from './voc-topic-detail'

export type PrimaryIntent = 'configuration_guidance' | 'functional_anomaly' | 'unclear'
export type DiagnosisStatus = 'reported' | 'pending' | 'config_cause_confirmed' | 'platform_limit_confirmed' | 'defect_confirmed' | 'not_applicable'
type SourceRef = { message_id: string | number; start: number; end: number }
export type Ref = { source: 'question' | 'answer'; quote: string; start: number; end: number; role: string; alignment: 'exact'; source_refs?: SourceRef[] }
export type Domain = { primary_intent: PrimaryIntent; fragment_role: 'request' | 'follow_up' | 'reproduction_context'; platforms: Ref[]; modules: Ref[]; merchant_goal: Ref | null; expected: Ref | null; actual: Ref | null; reproduction_context: Ref[]; prerequisites: Ref[]; operation_path: Ref[]; diagnosis: { status: DiagnosisStatus; cause_code: string; cause: Ref | null; evidence: Ref[] }; actions: { method_code: string; instruction: Ref; effect_status: 'suggested' | 'verified'; verification: Ref | null }[]; tags: string[] }
export type Topic = { id: string; question: string; theme: string; primary_intent?: PrimaryIntent; consultation_count?: number; event_count: number; customer_count: number; session_count: number; mention_count: number; last_seen: string; samples: string[]; platforms?: string[]; merchant_count?: number; unknown_merchant_sessions?: number; trend?: TopicTrend }
export type KnowledgeReuse = 'direct' | 'reference' | 'history' | 'blocked'
export type Knowledge = { reuse_level?: KnowledgeReuse; id: string; topic_id: string; variant_id?: string; kind?: VocKnowledgeKind; diagnosis_status?: DiagnosisStatus; effect_status?: 'suggested' | 'verified'; applicability?: Ref[]; confirmed_cause?: Ref | null; conversation_verification?: VocConversationVerification; knowledge_reuse?: VocKnowledgeReuse; question: string; answer: string; scope: string; steps: string[]; cautions: string[]; reply_patterns: string[]; evidence_count: number; status: 'ready'; version: number; updated_at: string; evidence: Evidence[] }
export type Evidence = { id: string; event_id: string; qa_id?: number | string; question: string; answer: string; question_quote?: string; customer_name?: string; customer_id?: string; group_name?: string; staff_name?: string; asked_at?: string | number; asked_at_text?: string; session_id?: string; domain?: Domain; staff_source_verified: boolean; question_message_ids?: (string | number)[]; workbench?: { commerce_settings: Ref[]; scenario_conditions: Ref[]; actual: Ref[]; question_message_id?: string; suspected_report_ready?: boolean }; claims: { role: string; quote: string; start: number; end: number; source_refs: SourceRef[] }[] }
type ResultList<T> = { items: T[]; total: number }
type TopicList = ResultList<Topic> & { comparison?: TrendComparisonData }
type Job = { id: string; status: string; total: number; failed: number; skipped: number; processed: number; error: string; created_at: string; updated_at: string; usage_tokens: number }
type Summary = { source_pairs: number; total_events: number; consultation_count?: number; topic_count: number; coverage: { processed_pairs: number; pending_pairs: number }; semantics: { ready: boolean; error?: string }; latest_job: Job | null }
export type OpenEvidencePair = (pair: QAPair, messageId?: string) => void
type WorkspaceProps = { filters: ApiFilters; refreshKey?: number; onOpenPair: OpenEvidencePair }
export type TopicDetail = { topic: Topic; events: Evidence[]; workbench?: { comparison: TrendComparisonData; knowledge: Knowledge[]; gaps: { code: string; count: number }[] } }
const topicPageSize = 20
const count = (n: number | undefined) => typeof n === 'number' ? n.toLocaleString('zh-CN') : '—'
const active = (job: Job | null | undefined) => job?.status === 'queued' || job?.status === 'running'
const labels: Record<string, string> = { queued: '排队中', running: '整理中', completed: '已完成', partial: '部分完成', failed: '失败', skipped: '已跳过' }
export function QueryPanel({ loading, error, retry, children, hasData = false }: { loading: boolean; error: string | null; retry: () => void; children: ReactNode; hasData?: boolean }) {
  if (loading) return <LoadingState />
  if (error && !hasData) return <div role="alert" className="flex min-h-40 flex-col items-center justify-center gap-3 text-center text-sm text-muted-foreground"><CircleAlert className="size-5" /><p>{error}</p><Button variant="outline" size="sm" onClick={retry}><RefreshCw />重试</Button></div>
  return <>{error && <div role="status" className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border bg-muted/20 p-3 text-sm text-muted-foreground"><CircleAlert className="size-4" /><span>自动更新暂时失败，正在显示上次结果。{error}</span><Button variant="ghost" size="xs" onClick={retry}>重试</Button></div>}{children}</>
}

export function VocFrequencyWorkspace({ filters, refreshKey = 0, onOpenPair, onOpenChat, initialTopic = null, onCloseLinkedTopic }: WorkspaceProps & { onOpenChat: (filters: ApiFilters) => void; initialTopic?: string | null; onCloseLinkedTopic?: () => void }) {
  const [supportIntent, setSupportIntent] = useState<NonNullable<ApiFilters['support_intent']>>('all')
  const [diagnosisStatus, setDiagnosisStatus] = useState<NonNullable<ApiFilters['diagnosis_status']>>('all')
  const [trend, setTrend] = useState<TrendFilter>('all')
  const [revision, setRevision] = useState(0)
  const [runOpen, setRunOpen] = useState(false)
  const [running, setRunning] = useState(false)
  const [limit, setLimit] = useState('100')
  const rankingHeading = useRef<HTMLDivElement>(null)
  const resultFilters: ApiFilters = { ...filters, support_intent: supportIntent, diagnosis_status: diagnosisStatus }
  const scopeKey = JSON.stringify({ ...resultFilters, trend })
  const [ranking, setRanking] = useState<{ scopeKey: string; page: number; selectedId: string | null }>({ scopeKey, page: 0, selectedId: initialTopic })
  const { page, selectedId } = ranking.scopeKey === scopeKey ? ranking : { page: 0, selectedId: null }
  const offset = page * topicPageSize
  const key = refreshKey + revision
  const summary = useApi<Summary>('voc2/summary', resultFilters, key)
  const topics = useApi<TopicList>('voc2/topics', { ...resultFilters, workbench: 1, trend, limit: topicPageSize, offset }, key)
  const detail = useApi<TopicDetail>('voc2/topic', { ...resultFilters, workbench: 1, id: selectedId ?? undefined }, refreshKey, selectedId !== null)
  const comparison = topics.data?.comparison
  const lastPage = topics.data ? Math.max(0, Math.ceil(topics.data.total / topicPageSize) - 1) : null
  const pageLoading = topics.loading || (lastPage !== null && page > lastPage)
  const job = summary.data?.latest_job
  const refreshResults = useCallback(() => setRevision(value => value + 1), [])
  const fetching = summary.loading || summary.refreshing || topics.loading || topics.refreshing
  useBackgroundRefresh(refreshResults, active(job), fetching, key)
  useEffect(() => {
    setRanking(current => current.scopeKey === scopeKey ? current : { scopeKey, page: 0, selectedId: null })
  }, [scopeKey])
  useEffect(() => {
    if (lastPage !== null && page > lastPage) setRanking(current => ({ ...current, page: lastPage }))
  }, [page, lastPage])

  function changePage(next: number) {
    setRanking({ scopeKey, page: next, selectedId: null })
    rankingHeading.current?.focus({ preventScroll: true })
    rankingHeading.current?.scrollIntoView?.({ block: 'start' })
  }

  async function run() {
    const n = Number(limit)
    if (!Number.isInteger(n) || n < 1 || n > 100) { toast.error('请输入 1 至 100 的整理条数。'); return }
    setRunning(true)
    try {
      await apiPost('voc2/run', filters, JSON.stringify({ limit: n }), { 'Content-Type': 'application/json' })
      setRunOpen(false); refreshResults(); toast.success('整理任务已提交。')
    } catch (error) { toast.error(error instanceof Error ? error.message : '提交失败，请重试。') }
    finally { setRunning(false) }
  }

  const metrics = [
    { label: '高频主题', value: summary.data?.topic_count },
    { label: '咨询次数', value: summary.data?.consultation_count },
    { label: '咨询片段', value: summary.data?.total_events },
  ]
  return <div className="space-y-6">
    <div className="flex flex-wrap items-center justify-between gap-4">
      <p className="text-sm text-muted-foreground">按商家咨询次数汇总场景配置、使用指导和功能排障问题。</p>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" onClick={() => onOpenChat(resultFilters)}><MessagesSquare />问问数据</Button>
        <Button onClick={() => setRunOpen(true)} disabled={active(job)}>{active(job) ? <LoadingMark /> : <Sparkles />}{active(job) ? '正在整理' : 'AI 整理当前范围'}</Button>
      </div>
    </div>
    <div className="flex flex-wrap items-center gap-3">
      <Select value={supportIntent} onValueChange={value => setSupportIntent(value as NonNullable<ApiFilters['support_intent']>)}>
        <SelectTrigger className="w-full sm:w-[205px]" aria-label="支持类型"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="all">全部支持类型</SelectItem>
          <SelectItem value="configuration_guidance">场景配置与使用指导</SelectItem>
          <SelectItem value="functional_anomaly">功能异常与排障</SelectItem>
          <SelectItem value="unclear">待识别背景</SelectItem>
        </SelectContent>
      </Select>
      <Select value={diagnosisStatus} onValueChange={value => setDiagnosisStatus(value as NonNullable<ApiFilters['diagnosis_status']>)}>
        <SelectTrigger className="w-full sm:w-[180px]" aria-label="诊断状态"><SelectValue /></SelectTrigger>
        <SelectContent><SelectItem value="all">全部诊断状态</SelectItem>{(Object.entries(diagnosisLabels) as [DiagnosisStatus, string][]).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
      </Select>
    </div>
    <div className="grid gap-3 sm:grid-cols-3">{metrics.map(({ label, value }) => <Card key={label}>
      <CardHeader><CardDescription>{label}</CardDescription><CardTitle className="text-2xl tabular-nums">{summary.loading ? <LoadingValue /> : count(value)}</CardTitle></CardHeader>
    </Card>)}</div>
    {summary.error && <QueryPanel hasData={summary.data !== null} loading={false} error={summary.error} retry={summary.retry}>{null}</QueryPanel>}
    {summary.data && !summary.data.semantics.ready && <div role="status" className="flex items-start gap-2 rounded-lg border bg-muted/30 p-4 text-sm text-muted-foreground"><CircleAlert className="mt-0.5 size-4 shrink-0" /><span>语义整理暂不可用{summary.data.semantics.error ? `：${summary.data.semantics.error}` : '，请检查模型网关配置。'}</span></div>}
    {job && <Card className="gap-3">
      <CardHeader><CardTitle className="text-sm">最近整理任务 <Badge variant="outline" className="ml-2 gap-1.5 font-normal">{active(job) && <LoadingMark className="size-3" />}{labels[job.status] || job.status}</Badge></CardTitle><CardDescription>按任务提交时的范围统计。</CardDescription></CardHeader>
      <CardContent>
        <Progress value={job.total ? Math.min(100, (job.processed + job.failed + job.skipped) / job.total * 100) : 0} className="mb-3 h-1.5" aria-label={`已尝试 ${job.processed + job.failed + job.skipped}/${job.total} 条问答`} />
        <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm tabular-nums"><span>总数 {count(job.total)}</span><span>成功 {count(job.processed)}</span><span>失败 {count(job.failed)}</span><span>跳过 {count(job.skipped)}</span></div>
        {job.error && <p role="alert" className="mt-2 text-sm text-destructive">{job.error}</p>}
      </CardContent>
    </Card>}
    <Card>
      <CardHeader>
        <CardTitle ref={rankingHeading} role="heading" aria-level={2} tabIndex={-1} className="scroll-mt-4">咨询主题排行</CardTitle>
        <CardDescription>按咨询次数排序，同一会话的同一主题去重。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div role="group" aria-label="趋势筛选" className="flex w-fit max-w-full flex-wrap gap-1 rounded-lg border bg-muted/35 p-1">
          {(Object.entries(trendLabels) as [TrendFilter, string][]).map(([value, label]) => <Button key={value} type="button" size="sm" variant={trend === value ? 'default' : 'ghost'} aria-pressed={trend === value}
            disabled={value !== 'all' && value !== trend && comparison?.comparable === false} onClick={() => setTrend(value)}>{label}</Button>)}
        </div>
        {comparison ? <TrendComparison comparison={comparison} /> : summary.data && <p className="text-xs text-muted-foreground">当前范围已整理 {count(summary.data.coverage.processed_pairs)} / {count(summary.data.source_pairs)} 条问答，待整理 {count(summary.data.coverage.pending_pairs)} 条。</p>}
        <QueryPanel hasData={topics.data !== null} loading={pageLoading} error={topics.error} retry={topics.retry}>
          {topics.data?.items.length ? <div className="divide-y">{topics.data.items.map((item, index) => {
            const badge = trendBadgeText(item.trend)
            const platforms = item.platforms?.slice(0, 3).join('、')
            return <button type="button" key={item.id} onClick={() => setRanking({ scopeKey, page, selectedId: item.id })} className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-sm px-1 py-4 text-left transition-colors hover:bg-muted/35 focus-visible:outline-2 focus-visible:outline-ring sm:px-2">
              <span className="flex min-w-0 items-start gap-3">
                <span className="w-7 shrink-0 text-right text-sm tabular-nums text-muted-foreground"><span className="sr-only">排名 </span>{count(offset + index + 1)}</span>
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  <span className="flex flex-wrap items-center gap-1.5"><span className="font-medium">{item.theme.trim() || item.question}</span>{badge && <Badge variant="outline" className="font-normal">{badge}</Badge>}</span>
                  {item.theme.trim() && item.theme.trim() !== item.question.trim() && <span className="mt-1 line-clamp-2 text-xs text-muted-foreground">{item.question}</span>}
                  <span className="mt-1 block text-xs leading-5 text-muted-foreground">{[intentLabels[item.primary_intent || 'unclear'], platforms, item.last_seen && `最近 ${displayTime(item.last_seen)}`].filter(Boolean).join(' · ')}</span>
                </span>
              </span>
              <span className="flex items-center gap-3 sm:gap-4">
                <span className="grid justify-items-end gap-1 text-right text-xs tabular-nums text-muted-foreground">
                  <span className="font-medium text-foreground">{count(item.consultation_count)} 次咨询</span>
                  <span className="flex flex-col items-end gap-x-1 sm:flex-row"><span>{count(item.session_count)} 个会话</span>{item.merchant_count !== undefined && <><span aria-hidden="true" className="hidden sm:inline">·</span><span>识别 {count(item.merchant_count)} 家商家</span></>}</span>
                </span>
                <ArrowUpRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
              </span>
            </button>
          })}</div> : <p className="py-10 text-center text-sm text-muted-foreground">{trend === 'all' ? '当前范围暂无高频问题。' : comparison?.comparable === false ? '当前覆盖不足，暂无法筛选趋势。' : `当前范围暂无「${trendLabels[trend]}」问题。`}</p>}
        </QueryPanel>
      </CardContent>
      {(pageLoading || page > 0 || Boolean(topics.data?.total)) && <CardFooter className="flex-wrap justify-between gap-3 border-t text-xs text-muted-foreground">
        <span>{topics.data ? `共 ${count(topics.data.total)} 个主题` : ''}</span>
        <nav aria-label="高频问题分页" className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" disabled={pageLoading || page === 0} onClick={() => changePage(page - 1)}><ChevronLeft />上一页</Button>
          <span aria-live="polite" className="tabular-nums">第 {page + 1}{lastPage !== null && !pageLoading ? ` / ${lastPage + 1}` : ''} 页</span>
          <Button variant="outline" size="sm" disabled={pageLoading || lastPage === null || page >= lastPage} onClick={() => changePage(page + 1)}>下一页<ChevronRight /></Button>
        </nav>
      </CardFooter>}
    </Card>
    <Sheet open={selectedId !== null} onOpenChange={open => {
      if (open) return
      setRanking(current => ({ ...current, selectedId: null }))
      if (initialTopic) onCloseLinkedTopic?.()
    }}>
      <SheetContent className="w-full gap-0 sm:max-w-3xl xl:max-w-4xl">
        <SheetHeader className="shrink-0 border-b px-4 sm:px-6"><SheetTitle>问题与原句依据</SheetTitle><SheetDescription className="sr-only">查看问题的处理判断、办法和会话依据。</SheetDescription></SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
          <QueryPanel hasData={detail.data !== null} loading={detail.loading} error={detail.error} retry={detail.retry}>{detail.data &&
            <TopicWorkbench key={detail.data.topic.id} detail={detail.data} onOpenPair={onOpenPair} onAskTopic={() => {
              const focusTopic = detail.data?.topic.id
              if (focusTopic) onOpenChat({ ...resultFilters, focus_topic: focusTopic })
            }} />
          }</QueryPanel>
        </div>
      </SheetContent>
    </Sheet>
    <Dialog open={runOpen} onOpenChange={setRunOpen}>
      <DialogContent>
        <DialogHeader><DialogTitle>整理当前范围</DialogTitle><DialogDescription>按顶部日期、客户群和客服条件提交新的整理任务。</DialogDescription></DialogHeader>
        <label className="space-y-2 text-sm"><span>最多整理问答数（1–100）</span><Input type="number" min="1" max="100" value={limit} onChange={event => setLimit(event.target.value)} /></label>
        <DialogFooter><Button variant="outline" onClick={() => setRunOpen(false)}>取消</Button><Button onClick={run} disabled={running}>{running && <LoaderCircle className="animate-spin" />}开始整理</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
}

export function VocKnowledgeWorkspace({ filters, refreshKey = 0, onOpenPair }: WorkspaceProps) {
  const levels = [
    { value: 'all', label: '全部' },
    { value: 'verified', label: '效果已验证' },
    { value: 'guidance', label: '已核验指引' },
    { value: 'reference', label: '来源记录' },
  ] as const
  const [revision, setRevision] = useState(0)
  const [referencesOpen, setReferencesOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [level, setLevel] = useState<typeof levels[number]['value']>('all')
  const key = refreshKey + revision
  const knowledge = useApi<ResultList<Knowledge>>('voc2/knowledge', filters, key)
  const refreshResults = useCallback(() => setRevision(value => value + 1), [])
  useBackgroundRefresh(refreshResults, false, knowledge.loading || knowledge.refreshing, key)
  const terms = search.normalize('NFKC').toLowerCase().split(/\s+/).filter(Boolean)
  const filtered = terms.length > 0 || level !== 'all'
  const matches = (knowledge.data?.items ?? []).filter(item => {
    if (item.status !== 'ready') return false
    const reuse = knowledgeReuseLevel(item)
    if (reuse === 'reference' && item.knowledge_reuse?.level !== 'reference') return false
    if (level !== 'all' && reuse !== level) return false
    if (terms.length === 0) return true
    const content = [item.question, item.answer, item.scope, ...item.steps].join('\n').normalize('NFKC').toLowerCase()
    return terms.every(term => content.includes(term))
  })
  const reusableItems = matches.filter(item => knowledgeReuseLevel(item) !== 'reference')
  const referenceItems = matches.filter(item => item.knowledge_reuse?.level === 'reference')
  const referenceCards = <div className="mt-4 grid gap-4 lg:grid-cols-2">{referenceItems.map(item =>
    <article key={item.id} className="min-w-0 rounded-lg border p-4"><KnowledgeContent item={item} onOpenPair={onOpenPair} /></article>
  )}</div>

  return <Card>
    <CardHeader>
      <CardTitle>可复用答案</CardTitle>
      <CardDescription>自动核验客服原话、适用条件和操作反馈，沉淀配置指引与处理方法。</CardDescription>
    </CardHeader>
    <CardContent>
      <QueryPanel hasData={knowledge.data !== null} loading={knowledge.loading} error={knowledge.error} retry={knowledge.retry}>
        <div className="mb-4 flex flex-col gap-3 border-b pb-4 lg:flex-row lg:items-center lg:justify-between">
          <Input type="search" value={search} onChange={event => setSearch(event.target.value)} maxLength={200}
            placeholder="搜索问题、答复、适用范围或步骤" aria-label="搜索知识" className="w-full lg:max-w-sm" />
          <div className="flex flex-wrap gap-1" role="group" aria-label="知识证据等级">
            {levels.map(option => <Button key={option.value} type="button" variant={level === option.value ? 'secondary' : 'ghost'}
              size="sm" aria-pressed={level === option.value} onClick={() => setLevel(option.value)}>{option.label}</Button>)}
          </div>
        </div>
        {filtered && matches.length > 0 && <p role="status" className="mb-3 text-sm text-muted-foreground">找到 {count(matches.length)} 条知识</p>}
        {filtered && matches.length === 0 && <div className="flex min-h-44 flex-col items-center justify-center gap-3 py-8 text-center text-sm text-muted-foreground">
          <p className="font-medium text-foreground">未找到符合条件的知识</p>
          <Button variant="outline" size="sm" onClick={() => {
            setSearch('')
            setLevel('all')
          }}>清除筛选</Button>
        </div>}
        {reusableItems.length ? <div className="grid gap-4 lg:grid-cols-2">{reusableItems.map(item =>
          <article key={item.id} className="min-w-0 space-y-3 rounded-lg border p-4">
            <div className="flex justify-end"><Button variant="outline" size="sm" onClick={() => copyKnowledge(item)}><Copy />{knowledgeReuseLevel(item) === 'guidance' ? '复制指引' : '复制处理办法'}</Button></div>
            <KnowledgeContent item={item} onOpenPair={onOpenPair} />
          </article>
        )}</div> : !filtered && <div className="flex min-h-44 flex-col items-center justify-center gap-2 py-8 text-center text-sm text-muted-foreground">
          <BookOpen aria-hidden="true" className="mb-1 size-5" />
          <p className="font-medium text-foreground">当前范围暂无可复用答案</p>
          <p>有足够证据的指引和处理方法会自动出现。</p>
        </div>}
        {referenceItems.length > 0 && (filtered ? <section className="mt-4 rounded-lg border p-4" aria-label="来源记录">
          <h3 className="text-sm font-medium">来源记录（{count(referenceItems.length)}）</h3>
          {referenceCards}
        </section> : <details className="mt-4 rounded-lg border p-4" open={referencesOpen} onToggle={event => setReferencesOpen(event.currentTarget.open)}>
          <summary className="w-fit cursor-pointer text-sm font-medium">来源记录（{count(referenceItems.length)}）</summary>
          {referencesOpen && referenceCards}
        </details>)}
      </QueryPanel>
    </CardContent>
  </Card>
}

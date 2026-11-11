import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowUpRight, BookOpen, ChevronLeft, ChevronRight, CircleAlert, LoaderCircle, MessagesSquare, RefreshCw, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import { useApi } from '@/hooks/use-api'
import { useBackgroundRefresh } from '@/hooks/use-background-refresh'
import { apiPost, normalizePair, type ApiFilters, type VocConversationVerification, type VocKnowledgeKind, type VocKnowledgeReuse } from '@/lib/api'
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

type PrimaryIntent = 'configuration_guidance' | 'functional_anomaly' | 'unclear'
type DiagnosisStatus = 'reported' | 'pending' | 'config_cause_confirmed' | 'platform_limit_confirmed' | 'defect_confirmed' | 'not_applicable'
type Ref = { source: 'question' | 'answer'; quote: string; start: number; end: number; role: string; alignment: 'exact' }
type Domain = { primary_intent: PrimaryIntent; fragment_role: 'request' | 'follow_up' | 'reproduction_context'; platforms: Ref[]; modules: Ref[]; merchant_goal: Ref | null; expected: Ref | null; actual: Ref | null; reproduction_context: Ref[]; prerequisites: Ref[]; operation_path: Ref[]; diagnosis: { status: DiagnosisStatus; cause_code: string; cause: Ref | null; evidence: Ref[] }; actions: { method_code: string; instruction: Ref; effect_status: 'suggested' | 'verified'; verification: Ref | null }[]; tags: string[] }
type Topic = { id: string; question: string; theme: string; primary_intent?: PrimaryIntent; consultation_count?: number; event_count: number; customer_count: number; session_count: number; mention_count: number; last_seen: string; samples: string[] }
export type Knowledge = { id: string; topic_id: string; variant_id?: string; kind?: VocKnowledgeKind; diagnosis_status?: DiagnosisStatus; effect_status?: 'suggested' | 'verified'; applicability?: Ref[]; confirmed_cause?: Ref | null; conversation_verification?: VocConversationVerification; knowledge_reuse?: VocKnowledgeReuse; question: string; answer: string; scope: string; steps: string[]; cautions: string[]; reply_patterns: string[]; evidence_count: number; status: 'ready'; version: number; updated_at: string; evidence: Evidence[] }
export type Evidence = { id: string; event_id: string; qa_id?: number | string; question: string; answer: string; question_quote?: string; customer_name?: string; customer_id?: string; group_name?: string; staff_name?: string; asked_at?: string | number; asked_at_text?: string; session_id?: string; domain?: Domain; staff_source_verified: boolean; claims: { role: string; quote: string; start: number; end: number; source_refs: { message_id: string | number; start: number; end: number }[] }[] }
type ResultList<T> = { items: T[]; total: number }
type Job = { id: string; status: string; total: number; failed: number; skipped: number; processed: number; error: string; created_at: string; updated_at: string; usage_tokens: number }
type Summary = { source_pairs: number; total_events: number; consultation_count?: number; topic_count: number; coverage: { processed_pairs: number; pending_pairs: number }; semantics: { ready: boolean; error?: string }; latest_job: Job | null }
type WorkspaceProps = { filters: ApiFilters; refreshKey?: number; onOpenPair: (pair: QAPair) => void }
export type TopicDetail = { topic: Topic; events: Evidence[] }
const topicPageSize = 20
const count = (n: number | undefined) => typeof n === 'number' ? n.toLocaleString('zh-CN') : '—'
const active = (job: Job | null | undefined) => job?.status === 'queued' || job?.status === 'running'
const labels: Record<string, string> = { queued: '排队中', running: '整理中', completed: '已完成', partial: '部分完成', failed: '失败', skipped: '已跳过' }
const intentLabels: Record<PrimaryIntent, string> = { configuration_guidance: '场景配置与使用指导', functional_anomaly: '功能异常与排障', unclear: '待识别背景' }
const diagnosisLabels: Record<DiagnosisStatus, string> = { reported: '商家报告', pending: '待定位', config_cause_confirmed: '配置原因已确认', platform_limit_confirmed: '平台约束已确认', defect_confirmed: '缺陷已确认', not_applicable: '不适用' }
const knowledgeLabels: Record<VocKnowledgeKind, string> = { configuration_guide: '配置指引', troubleshooting_advice: '排查建议', verified_workaround: '绕行方案', capability_fact: '功能说明' }
const reuseLabels: Record<VocKnowledgeReuse['level'], string> = { verified: '操作效果已验证', guidance: '来源已核验', reference: '来源记录' }

function knowledgeReuseLevel(item: Knowledge): VocKnowledgeReuse['level'] {
  if (item.knowledge_reuse) return item.knowledge_reuse.level
  const closure = item.conversation_verification
  return closure?.auto_reusable && closure.status === 'accepted' && closure.acceptance === 'resolved'
    && closure.effect === 'customer_reported_success' ? 'verified' : 'reference'
}

function displayTime(input?: string | number): string {
  if (!input) return ''
  const value = String(input)
  if (!/^\d+$/.test(value)) return value
  const parsed = Number(value)
  const date = new Date(parsed < 100_000_000_000 ? parsed * 1000 : parsed)
  return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date).replaceAll('/', '-')
}

export function QueryPanel({ loading, error, retry, children, hasData = false }: { loading: boolean; error: string | null; retry: () => void; children: ReactNode; hasData?: boolean }) {
  if (loading) return <LoadingState />
  if (error && !hasData) return <div role="alert" className="flex min-h-40 flex-col items-center justify-center gap-3 text-center text-sm text-muted-foreground"><CircleAlert className="size-5" /><p>{error}</p><Button variant="outline" size="sm" onClick={retry}><RefreshCw />重试</Button></div>
  return <>{error && <div role="status" className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border bg-muted/20 p-3 text-sm text-muted-foreground"><CircleAlert className="size-4" /><span>自动更新暂时失败，正在显示上次结果。{error}</span><Button variant="ghost" size="xs" onClick={retry}>重试</Button></div>}{children}</>
}

function SourceQuotes({ label, refs }: { label: string; refs: (Ref | null | undefined)[] }) {
  const present = refs.filter((ref): ref is Ref => Boolean(ref?.quote))
  if (!present.length) return null
  return <div className="space-y-1"><div className="text-xs font-medium text-foreground">{label}</div>{present.map((ref, index) => <p key={index} className="whitespace-pre-wrap break-words text-sm text-muted-foreground">“{ref.quote}” <span className="text-xs">· {ref.source === 'question' ? '商家原话' : '回复原话'}</span></p>)}</div>
}

function DomainEvidence({ domain }: { domain?: Domain }) {
  if (!domain) return null
  const status = domain.diagnosis?.status
  return <div className="mt-3 space-y-3 border-t pt-3">
    <div className="flex flex-wrap gap-2"><Badge variant="secondary" className="font-normal">{intentLabels[domain.primary_intent] || '待识别背景'}</Badge>{status && status !== 'not_applicable' && <Badge variant="outline" className="font-normal">{diagnosisLabels[status] || '待核对'}</Badge>}{domain.fragment_role === 'reproduction_context' && <Badge variant="outline" className="font-normal">复现背景</Badge>}{domain.fragment_role === 'follow_up' && <Badge variant="outline" className="font-normal">后续补充</Badge>}</div>
    <SourceQuotes label="涉及平台" refs={domain.platforms || []} />
    <SourceQuotes label="涉及模块" refs={domain.modules || []} />
    <SourceQuotes label="商家诉求" refs={[domain.merchant_goal]} />
    <SourceQuotes label="期望结果" refs={[domain.expected]} />
    <SourceQuotes label="实际表现" refs={[domain.actual]} />
    <SourceQuotes label="诊断原因" refs={[domain.diagnosis?.cause]} />
    <SourceQuotes label="诊断依据" refs={domain.diagnosis?.evidence || []} />
    <SourceQuotes label="前置条件" refs={domain.prerequisites || []} />
    <SourceQuotes label="原文操作步骤" refs={domain.operation_path || []} />
    <SourceQuotes label="复现背景" refs={domain.reproduction_context || []} />
    {(domain.actions || []).map((action, index) => <div key={index} className="space-y-1"><SourceQuotes label={action.effect_status === 'verified' && action.verification?.quote ? '已验证操作' : '建议操作（效果未验证）'} refs={[action.instruction]} /><SourceQuotes label="验证依据" refs={[action.verification]} /></div>)}
  </div>
}

export function EvidenceList({ evidence, onOpenPair }: { evidence: Evidence[]; onOpenPair: (pair: QAPair) => void }) {
  if (!evidence.length) return <p className="text-sm text-muted-foreground">暂无可展示的原句依据。</p>
  return <div className="space-y-3">{evidence.map(item => {
    const quotes = [...new Set(item.claims.filter(claim => claim.role === 'staff' && claim.quote.trim()).map(claim => claim.quote))]
    return <div key={item.id} className="rounded-lg border bg-muted/20 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{item.qa_id ? `问答 ${item.qa_id} · ` : ''}{item.customer_name || '匿名客户'}{displayTime(item.asked_at) ? ` · ${displayTime(item.asked_at)}` : ''}</span>{item.qa_id && <Button variant="ghost" size="xs" onClick={() => onOpenPair(normalizePair(item))}>查看会话上下文 <ArrowUpRight /></Button>}</div>
      <p className="mt-2 whitespace-pre-wrap break-words text-sm">{item.question_quote || item.question}</p>
      <DomainEvidence domain={item.domain} />
      <div className="mt-3 space-y-1.5 text-sm"><div className="flex flex-wrap items-center gap-2"><span className="font-medium">{quotes.length && item.staff_source_verified ? '本意图客服原句' : '本意图回复依据'}</span>{(!item.staff_source_verified || quotes.length > 0) && <Badge variant="outline" className="font-normal">{item.staff_source_verified ? '客服来源已核验' : '客服来源未核验'}</Badge>}</div>{quotes.length ? quotes.map((quote, index) => <p key={index} className="whitespace-pre-wrap break-words text-muted-foreground">{quote}</p>) : <p className="text-muted-foreground">暂无本意图的回复片段。</p>}</div>
    </div>
  })}</div>
}

export function KnowledgeContent({ item, onOpenPair }: { item: Knowledge; onOpenPair: (pair: QAPair) => void }) {
  const kind = item.kind ? knowledgeLabels[item.kind] : '类型待识别'
  const confirmed = item.diagnosis_status && ['config_cause_confirmed', 'platform_limit_confirmed', 'defect_confirmed'].includes(item.diagnosis_status)
  const closure = item.conversation_verification
  const feedbackEvidence = item.knowledge_reuse?.evidence ?? closure?.evidence
  const level = knowledgeReuseLevel(item)
  const guidanceDescription = item.kind === 'configuration_guide'
    ? '客服原话和适用条件已核验，可按所列条件使用。'
    : '客服原话和适用条件已核验，可用于排查。操作效果待验证。'
  const reuseDescription = level === 'verified' ? '客户明确反馈操作成功，可按所列条件复用。'
    : level === 'guidance' ? guidanceDescription : '保留此记录供查阅原始会话。'
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center gap-2"><Badge variant="secondary" className="font-normal">{kind}</Badge><Badge variant={level === 'verified' ? 'default' : 'outline'} className="font-normal">{reuseLabels[level]}</Badge>{item.diagnosis_status && item.diagnosis_status !== 'not_applicable' && <Badge variant="outline" className="font-normal">{diagnosisLabels[item.diagnosis_status] || '待核对'}</Badge>}</div>
    <div className="rounded-lg border bg-muted/20 p-3 text-sm">
      <p>{reuseDescription}</p>
      {closure?.acceptance === 'understood' && <p className="mt-1 text-muted-foreground">客户已接受解释。</p>}
      {feedbackEvidence?.map((ref, index) => <p key={index} className="mt-2 whitespace-pre-wrap break-words text-muted-foreground">“{ref.quote}” · {ref.role === 'user' || ref.role === 'merchant' ? '客户反馈' : '会话证据'}{ref.ts ? ` · ${displayTime(ref.ts)}` : ''}{ref.message_id ? ` · 消息 ${ref.message_id}` : ''}</p>)}
    </div>
    <h3 className="font-medium">{item.question}</h3><p className="whitespace-pre-wrap break-words text-sm">{item.answer}</p>
    {item.steps.length > 0 && <div className="text-sm"><span className="font-medium">步骤</span><ol className="mt-1 list-inside list-decimal space-y-1">{item.steps.map((step, i) => <li key={i}>{step}</li>)}</ol></div>}
    {item.scope && <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">适用范围：{item.scope}</p>}
    <SourceQuotes label="适用条件依据" refs={item.applicability || []} />
    {confirmed && <SourceQuotes label="已确认原因依据" refs={[item.confirmed_cause]} />}
    {item.reply_patterns.length > 0 && <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">日常话术：{item.reply_patterns.join('；')}</p>}
    {item.cautions.length > 0 && <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">注意事项：{item.cautions.join('；')}</p>}
    <div><h4 className="mb-2 text-sm font-medium">来源依据</h4><EvidenceList evidence={item.evidence} onOpenPair={onOpenPair} /></div>
  </div>
}

export function VocFrequencyWorkspace({ filters, refreshKey = 0, onOpenPair, onOpenChat }: WorkspaceProps & { onOpenChat: (filters: ApiFilters) => void }) {
  const [supportIntent, setSupportIntent] = useState<NonNullable<ApiFilters['support_intent']>>('all')
  const [diagnosisStatus, setDiagnosisStatus] = useState<NonNullable<ApiFilters['diagnosis_status']>>('all')
  const [revision, setRevision] = useState(0)
  const [runOpen, setRunOpen] = useState(false)
  const [running, setRunning] = useState(false)
  const [limit, setLimit] = useState('100')
  const rankingHeading = useRef<HTMLDivElement>(null)
  const resultFilters: ApiFilters = { ...filters, support_intent: supportIntent, diagnosis_status: diagnosisStatus }
  const scopeKey = JSON.stringify(resultFilters)
  const [ranking, setRanking] = useState<{ scopeKey: string; page: number; selectedId: string | null }>({ scopeKey, page: 0, selectedId: null })
  const { page, selectedId } = ranking.scopeKey === scopeKey ? ranking : { page: 0, selectedId: null }
  const offset = page * topicPageSize
  const key = refreshKey + revision
  const summary = useApi<Summary>('voc2/summary', resultFilters, key)
  const topics = useApi<ResultList<Topic>>('voc2/topics', { ...resultFilters, limit: topicPageSize, offset }, key)
  const detail = useApi<TopicDetail>('voc2/topic', { ...resultFilters, id: selectedId ?? undefined }, refreshKey, selectedId !== null)
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
        <CardDescription>按咨询次数排序。同一次咨询的多次提及可能产生多个片段。</CardDescription>
        {summary.data && <p className="text-xs text-muted-foreground">当前日期、客户群和客服范围已整理 {count(summary.data.coverage.processed_pairs)} / {count(summary.data.source_pairs)} 条问答，待整理 {count(summary.data.coverage.pending_pairs)} 条。</p>}
      </CardHeader>
      <CardContent>
        <QueryPanel hasData={topics.data !== null} loading={pageLoading} error={topics.error} retry={topics.retry}>
          {topics.data?.items.length ? <div className="divide-y">{topics.data.items.map((item, index) => <button key={item.id} onClick={() => setRanking({ scopeKey, page, selectedId: item.id })} className="flex w-full flex-wrap items-center justify-between gap-3 rounded-sm py-4 text-left hover:bg-muted/30 focus-visible:outline-2 focus-visible:outline-ring">
            <span className="flex min-w-0 items-start gap-3">
              <span className="w-7 shrink-0 text-right text-sm tabular-nums text-muted-foreground"><span className="sr-only">排名 </span>{count(offset + index + 1)}</span>
              <span className="min-w-0 break-words">
                <span className="block font-medium">{item.theme.trim() || item.question}</span>
                {item.theme.trim() && item.theme.trim() !== item.question.trim() && <span className="mt-1 block text-xs text-muted-foreground">代表问法：{item.question}</span>}
                <span className="mt-1 block text-xs text-muted-foreground">{intentLabels[item.primary_intent || 'unclear']} · 最近 {item.last_seen || '时间未知'}</span>
              </span>
            </span>
            <span className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs tabular-nums text-muted-foreground"><span>{count(item.consultation_count)} 次咨询</span><span>{count(item.event_count)} 个咨询片段</span><span>{count(item.customer_count)} 客户</span><span>{count(item.session_count)} 会话</span><ArrowUpRight aria-hidden="true" className="size-4" /></span>
          </button>)}</div> : <p className="py-10 text-center text-sm text-muted-foreground">当前范围暂无高频问题。</p>}
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
    <Sheet open={selectedId !== null} onOpenChange={open => { if (!open) setRanking(current => ({ ...current, selectedId: null })) }}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader><SheetTitle>问题与原句依据</SheetTitle><SheetDescription>按对应查询范围展示相关会话。</SheetDescription></SheetHeader>
        <div className="space-y-5 px-4 pb-6">
          <QueryPanel hasData={detail.data !== null} loading={detail.loading} error={detail.error} retry={detail.retry}>{detail.data && <>
            <div>
              <h3 className="text-lg font-semibold">{detail.data.topic.theme.trim() || detail.data.topic.question}</h3>
              <p className="mt-2 text-sm text-muted-foreground">代表问法：{detail.data.topic.question}</p>
              <p className="mt-3 text-sm tabular-nums">{count(detail.data.topic.consultation_count)} 次咨询 · {count(detail.data.topic.event_count)} 个咨询片段 · {count(detail.data.topic.customer_count)} 客户 · {count(detail.data.topic.session_count)} 会话</p>
            </div>
            <div><h4 className="mb-3 font-medium">原句依据</h4><EvidenceList evidence={detail.data.events} onOpenPair={onOpenPair} /></div>
          </>}</QueryPanel>
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
  const [revision, setRevision] = useState(0)
  const [referencesOpen, setReferencesOpen] = useState(false)
  const key = refreshKey + revision
  const knowledge = useApi<ResultList<Knowledge>>('voc2/knowledge', filters, key)
  const refreshResults = useCallback(() => setRevision(value => value + 1), [])
  useBackgroundRefresh(refreshResults, false, knowledge.loading || knowledge.refreshing, key)
  const readyItems = knowledge.data?.items.filter(item => item.status === 'ready') ?? []
  const reusableItems = readyItems.filter(item => knowledgeReuseLevel(item) !== 'reference')
  const referenceItems = readyItems.filter(item => item.knowledge_reuse?.level === 'reference')

  return <Card>
    <CardHeader>
      <CardTitle>可复用答案</CardTitle>
      <CardDescription>自动核验客服原话、适用条件和操作反馈，沉淀配置指引与处理方法。</CardDescription>
    </CardHeader>
    <CardContent>
      <QueryPanel hasData={knowledge.data !== null} loading={knowledge.loading} error={knowledge.error} retry={knowledge.retry}>
        {reusableItems.length ? <div className="grid gap-4 lg:grid-cols-2">{reusableItems.map(item =>
          <article key={item.id} className="min-w-0 rounded-lg border p-4"><KnowledgeContent item={item} onOpenPair={onOpenPair} /></article>
        )}</div> : <div className="flex min-h-44 flex-col items-center justify-center gap-2 py-8 text-center text-sm text-muted-foreground">
          <BookOpen aria-hidden="true" className="mb-1 size-5" />
          <p className="font-medium text-foreground">当前范围暂无可复用答案</p>
          <p>有足够证据的指引和处理方法会自动出现。</p>
        </div>}
        {referenceItems.length > 0 && <details className="mt-4 rounded-lg border p-4" open={referencesOpen} onToggle={event => setReferencesOpen(event.currentTarget.open)}>
          <summary className="w-fit cursor-pointer text-sm font-medium">来源记录（{count(referenceItems.length)}）</summary>
          {referencesOpen && <div className="mt-4 grid gap-4 lg:grid-cols-2">{referenceItems.map(item =>
            <article key={item.id} className="min-w-0 rounded-lg border p-4"><KnowledgeContent item={item} onOpenPair={onOpenPair} /></article>
          )}</div>}
        </details>}
      </QueryPanel>
    </CardContent>
  </Card>
}

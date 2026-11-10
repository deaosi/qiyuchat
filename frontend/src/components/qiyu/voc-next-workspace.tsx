import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowUpRight, Bot, CircleAlert, FileText, LoaderCircle, MessageSquareText, RefreshCw, Send, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import { useApi } from '@/hooks/use-api'
import { useBackgroundRefresh } from '@/hooks/use-background-refresh'
import { apiPost, normalizePair, type ApiFilters, type VocConversationVerification, type VocKnowledgeKind, type VocQueryFilters } from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

type PrimaryIntent = 'configuration_guidance' | 'functional_anomaly' | 'unclear'
type DiagnosisStatus = 'reported' | 'pending' | 'config_cause_confirmed' | 'platform_limit_confirmed' | 'defect_confirmed' | 'not_applicable'
type Ref = { source: 'question' | 'answer'; quote: string; start: number; end: number; role: string; alignment: 'exact' }
type Domain = { primary_intent: PrimaryIntent; fragment_role: 'request' | 'follow_up' | 'reproduction_context'; platforms: Ref[]; modules: Ref[]; merchant_goal: Ref | null; expected: Ref | null; actual: Ref | null; reproduction_context: Ref[]; prerequisites: Ref[]; operation_path: Ref[]; diagnosis: { status: DiagnosisStatus; cause_code: string; cause: Ref | null; evidence: Ref[] }; actions: { method_code: string; instruction: Ref; effect_status: 'suggested' | 'verified'; verification: Ref | null }[]; tags: string[] }
type Topic = { id: string; question: string; theme: string; primary_intent?: PrimaryIntent; consultation_count?: number; event_count: number; customer_count: number; session_count: number; mention_count: number; last_seen: string; samples: string[] }
type Knowledge = { id: string; topic_id: string; variant_id?: string; kind?: VocKnowledgeKind; diagnosis_status?: DiagnosisStatus; effect_status?: 'suggested' | 'verified'; applicability?: Ref[]; confirmed_cause?: Ref | null; conversation_verification?: VocConversationVerification; question: string; answer: string; scope: string; steps: string[]; cautions: string[]; reply_patterns: string[]; evidence_count: number; status: 'ready'; version: number; updated_at: string; evidence: Evidence[] }
type Exception = { id: string; topic_id?: string; question: string; kind: string; reason: string; created_at: string }
type Evidence = { id: string; event_id: string; qa_id?: number; question: string; answer: string; question_quote?: string; customer_name?: string; staff_name?: string; asked_at?: string; session_id?: string; domain?: Domain; staff_source_verified: boolean; claims: { role: string; quote: string; start: number; end: number; source_refs: { message_id: string | number; start: number; end: number }[] }[] }
type ResultList<T> = { items: T[]; total: number }
type Job = { id: string; status: string; total: number; failed: number; skipped: number; processed: number; error: string; created_at: string; updated_at: string; usage_tokens: number }
type Summary = { source_pairs: number; total_events: number; answered_events: number; topic_count: number; ready_knowledge: number; auto_reusable_knowledge?: number; exception_count: number; coverage: { processed_pairs: number; pending_pairs: number; answered_pairs: number }; semantics: { ready: boolean; model: string; error?: string }; latest_job: Job | null }
type TopicDetail = { topic: Topic; events: Evidence[] }
type Citation = { kind: 'topic' | 'knowledge' | 'event'; id: string; title: string }
type Answer = { answer: string; citations: Citation[]; tools: { name: string; label: string }[]; scope: { label: string; query_filters?: VocQueryFilters }; model: string }
type Message = { role: 'user' | 'assistant'; content: string; citations?: Citation[]; scope?: string; queryFilters?: VocQueryFilters; model?: string; tools?: { name: string; label: string }[] }
const projectionKeys = ['days', 'start', 'end', 'group', 'staff', 'customer', 'q', 'support_intent', 'diagnosis_status', 'replied', 'cluster'] as const
function citationFilters(base: ApiFilters, projection?: VocQueryFilters): ApiFilters {
  if (!projection) return base
  const allowed = Object.fromEntries(projectionKeys.filter(key => Object.hasOwn(projection, key)).map(key => [key, projection[key] ?? undefined]))
  return { ...base, ...allowed, source: 'local' }
}
const count = (n: number | undefined) => typeof n === 'number' ? n.toLocaleString('zh-CN') : '—'
const active = (job: Job | null | undefined) => job?.status === 'queued' || job?.status === 'running'
const labels: Record<string, string> = { queued: '排队中', running: '整理中', completed: '已完成', partial: '部分完成', failed: '失败' }
const intentLabels: Record<PrimaryIntent, string> = { configuration_guidance: '场景配置与使用指导', functional_anomaly: '功能异常与排障', unclear: '待识别背景' }
const diagnosisLabels: Record<DiagnosisStatus, string> = { reported: '商家报告', pending: '待定位', config_cause_confirmed: '配置原因已确认', platform_limit_confirmed: '平台约束已确认', defect_confirmed: '缺陷已确认', not_applicable: '不适用' }
const knowledgeLabels: Record<VocKnowledgeKind, string> = { configuration_guide: '配置指导', troubleshooting_advice: '排查建议（效果未验证）', verified_workaround: '已验证绕行', capability_fact: '功能说明' }
const exceptionLabels: Record<string, string> = {
  no_current_evidence: '来源需更新', no_staff_answer: '缺少客服回复', unverified_staff_source: '客服来源未核验',
  collect_info: '需要补充信息', no_resolved_answer: '缺少解决答复', mixed_answer_types: '答复类型不一致',
  source_conflict: '客服口径冲突', missing_conditions: '缺少适用条件', scope_conflict: '适用范围冲突',
  incomplete_answer: '答复不完整', incomplete_claim_coverage: '引文需要核对', condition_omitted: '缺少限定条件',
  number_conflict: '数值口径冲突', negation_conflict: '肯否口径冲突', policy_needs_support: '政策依据不足', answer_variation: '客服口径不一致',
}
function displayTime(value?: string): string {
  if (!value) return ''
  if (!/^\d+$/.test(value)) return value
  const parsed = Number(value)
  const date = new Date(parsed < 100_000_000_000 ? parsed * 1000 : parsed)
  return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date).replaceAll('/', '-')
}

function QueryPanel({ loading, error, retry, children, hasData = false }: { loading: boolean; error: string | null; retry: () => void; children: ReactNode; hasData?: boolean }) {
  if (loading) return <div role="status" className="flex min-h-40 items-center justify-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在读取…</div>
  if (error && !hasData) return <div role="alert" className="flex min-h-40 flex-col items-center justify-center gap-3 text-center text-sm text-muted-foreground"><CircleAlert className="size-5" /><p>{error}</p><Button variant="outline" size="sm" onClick={retry}><RefreshCw />重试</Button></div>
  return <>{error && <div role="status" className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border bg-muted/20 p-3 text-sm text-muted-foreground"><CircleAlert className="size-4" /><span>自动更新暂时失败，正在显示上次结果。{error}</span><Button variant="ghost" size="xs" onClick={retry}>重试</Button></div>}{children}</>
}

function AnswerContext({ message }: { message: Message }) {
  if (message.role !== 'assistant') return null
  const detail = [message.scope, message.model, message.tools?.map(tool => tool.label).join('、')].filter(Boolean).join(' · ')
  return detail ? <p className="mt-3 text-xs text-muted-foreground">{detail}</p> : null
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

function EvidenceList({ evidence, onOpenPair }: { evidence: Evidence[]; onOpenPair: (pair: QAPair) => void }) {
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

function KnowledgeContent({ item, onOpenPair }: { item: Knowledge; onOpenPair: (pair: QAPair) => void }) {
  const kind = item.kind === 'verified_workaround' && item.effect_status !== 'verified'
    ? '排查建议（效果未验证）' : item.kind ? knowledgeLabels[item.kind] : '类型待识别'
  const confirmed = item.diagnosis_status && ['config_cause_confirmed', 'platform_limit_confirmed', 'defect_confirmed'].includes(item.diagnosis_status)
  const closure = item.conversation_verification
  const coverageCount = (key: string) => count(typeof closure?.coverage[key] === 'number' ? closure.coverage[key] : undefined)
  const acceptance = closure?.auto_reusable
    ? closure.acceptance === 'resolved' ? '客户反馈操作成功' : closure.acceptance === 'understood' ? '客户已接受解答' : '多例闭环支持'
    : closure?.status === 'negative' ? '后续仍有疑问' : closure?.status === 'weak' ? '闭环证据待积累' : '闭环证据不足'
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center gap-2"><Badge variant="secondary" className="font-normal">{kind}</Badge><Badge variant={closure?.auto_reusable ? 'default' : 'outline'} className="font-normal">{acceptance}</Badge>{item.diagnosis_status && item.diagnosis_status !== 'not_applicable' && <Badge variant="outline" className="font-normal">{diagnosisLabels[item.diagnosis_status] || '待核对'}</Badge>}</div>
    <div className="rounded-lg border bg-muted/20 p-3 text-sm"><p>{closure?.auto_reusable ? '会话验证通过，按所列条件可直接复用。' : '客服原话有依据；尚不能作为客户已认可的最终答案。'}</p>{closure && <p className="mt-1 text-xs text-muted-foreground">完整来源 {coverageCount('complete_sources')} / {coverageCount('sources')} · 明确接受 {coverageCount('accepted_sources')} · 独立弱闭环 {coverageCount('independent_weak_supports')}（阈值 {coverageCount('weak_threshold')}）</p>}{closure?.acceptance === 'understood' && <p className="mt-1 text-muted-foreground">客户接受了解释，操作效果尚未据此确认。</p>}{closure?.evidence.map((ref, index) => <p key={index} className="mt-2 whitespace-pre-wrap break-words text-muted-foreground">“{ref.quote}” · {ref.role === 'user' || ref.role === 'merchant' ? '客户反馈' : '会话证据'}{ref.ts ? ` · ${displayTime(String(ref.ts))}` : ''}{ref.message_id ? ` · 消息 ${ref.message_id}` : ''}</p>)}</div>
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

export function VocNextWorkspace({ filters, refreshKey = 0, onOpenPair }: { filters: ApiFilters; refreshKey?: number; onOpenPair: (pair: QAPair) => void }) {
  const [tab, setTab] = useState('topics')
  const [knowledgeView, setKnowledgeView] = useState('verified')
  const [supportIntent, setSupportIntent] = useState<NonNullable<ApiFilters['support_intent']>>('all')
  const [diagnosisStatus, setDiagnosisStatus] = useState<NonNullable<ApiFilters['diagnosis_status']>>('all')
  const [revision, setRevision] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedEvent, setSelectedEvent] = useState<string | null>(null)
  const [selectedKnowledge, setSelectedKnowledge] = useState<string | null>(null)
  const [detailFilters, setDetailFilters] = useState<ApiFilters | null>(null)
  const [chatOpen, setChatOpen] = useState(false)
  const [runOpen, setRunOpen] = useState(false)
  const [running, setRunning] = useState(false)
  const [limit, setLimit] = useState('100')
  const [messages, setMessages] = useState<Message[]>([])
  const [question, setQuestion] = useState('')
  const [asking, setAsking] = useState(false)
  const [chatError, setChatError] = useState('')
  const resultFilters: ApiFilters = { ...filters, support_intent: supportIntent, diagnosis_status: diagnosisStatus }
  const scopeKey = JSON.stringify(resultFilters)
  const currentScope = useRef(scopeKey)
  currentScope.current = scopeKey
  const key = refreshKey + revision
  const summary = useApi<Summary>('voc2/summary', resultFilters, key)
  const topics = useApi<ResultList<Topic>>('voc2/topics', resultFilters, key, tab === 'topics')
  const knowledge = useApi<ResultList<Knowledge>>('voc2/knowledge', resultFilters, key, tab === 'knowledge')
  const exceptions = useApi<ResultList<Exception>>('voc2/exceptions', resultFilters, key, tab === 'exceptions')
  const defects = useApi<ResultList<Evidence>>('voc2/defects', resultFilters, key, tab === 'exceptions')
  // Open evidence is a reading view: background progress must not replace it.
  const detail = useApi<TopicDetail>('voc2/topic', { ...(detailFilters ?? resultFilters), id: selectedId ?? undefined }, refreshKey, selectedId !== null)
  const eventDetail = useApi<{ event: Evidence; topic?: Topic }>('voc2/event', { ...(detailFilters ?? resultFilters), id: selectedEvent ?? undefined }, refreshKey, selectedEvent !== null)
  const knowledgeDetail = useApi<{ item: Knowledge }>('voc2/knowledge', { ...(detailFilters ?? resultFilters), id: selectedKnowledge ?? undefined }, refreshKey, selectedKnowledge !== null)
  const job = summary.data?.latest_job
  const refreshResults = useCallback(() => setRevision(value => value + 1), [])
  const fetching = [summary, topics, knowledge, exceptions, defects].some(query => query.loading || query.refreshing)
  useBackgroundRefresh(refreshResults, active(job), fetching, key)
  useEffect(() => { setSelectedId(null); setSelectedEvent(null); setSelectedKnowledge(null); setDetailFilters(null); setMessages([]); setChatError(''); setQuestion('') }, [scopeKey])

  async function run() {
    const n = Number(limit)
    if (!Number.isInteger(n) || n < 1 || n > 100) { toast.error('请输入 1 至 100 的整理条数。'); return }
    setRunning(true)
    try {
      await apiPost('voc2/run', filters, JSON.stringify({ limit: n }), { 'Content-Type': 'application/json' })
      setRunOpen(false); setRevision(value => value + 1); toast.success('整理任务已提交。')
    } catch (error) { toast.error(error instanceof Error ? error.message : '提交失败，请重试。') }
    finally { setRunning(false) }
  }
  async function ask(event: React.FormEvent) {
    event.preventDefault()
    const text = question.trim()
    if (!text || asking) return
    const requestScope = scopeKey
    const history = messages.map(({ role, content }) => ({ role, content }))
    setMessages(current => [...current, { role: 'user', content: text }]); setQuestion(''); setChatError(''); setAsking(true)
    try {
      const result = await apiPost<Answer>('voc2/assistant', resultFilters, JSON.stringify({ message: text, history }), { 'Content-Type': 'application/json' })
      if (requestScope !== currentScope.current) return
      setMessages(current => [...current, { role: 'assistant', content: result.answer, citations: result.citations, scope: result.scope.label, queryFilters: result.scope.query_filters, model: result.model, tools: result.tools }])
    } catch (error) {
      if (requestScope === currentScope.current) {
        setMessages(current => current.slice(0, -1))
        setQuestion(text)
        setChatError(error instanceof Error ? error.message : '查询失败，请重试。')
      }
    }
    finally { setAsking(false) }
  }
  const openCitation = (citation: Citation, message: Message) => {
    setChatOpen(false)
    setDetailFilters(citationFilters(resultFilters, message.queryFilters))
    if (citation.kind === 'topic') setSelectedId(citation.id)
    if (citation.kind === 'knowledge') setSelectedKnowledge(citation.id)
    if (citation.kind === 'event') setSelectedEvent(citation.id)
  }

  return <div className="space-y-6">
    <div className="flex flex-wrap items-start justify-between gap-4"><div><h2 className="text-xl font-semibold tracking-tight">知汇鸽使用支持</h2><p className="mt-1 text-sm text-muted-foreground">根据七鱼中的商家咨询，了解场景配置、使用指导与功能排障。</p></div><div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => setChatOpen(true)}><MessageSquareText />问问数据</Button><Button onClick={() => setRunOpen(true)} disabled={active(job)}><Sparkles />{active(job) ? '正在整理' : 'AI 整理当前范围'}</Button></div></div>
    {summary.data && <div className="rounded-lg border bg-muted/20 px-4 py-3 text-sm text-muted-foreground">当前日期、客户群和客服筛选范围来源 <span className="font-medium tabular-nums text-foreground">{count(summary.data.source_pairs)}</span> 条，已整理 <span className="font-medium tabular-nums text-foreground">{count(summary.data.coverage.processed_pairs)}</span> 条，待整理 <span className="font-medium tabular-nums text-foreground">{count(summary.data.coverage.pending_pairs)}</span> 条；{summary.data.coverage.pending_pairs === 0 ? '已覆盖当前来源范围' : '结果统计基于已整理部分'}。含有效客服回复来源 {count(summary.data.coverage.answered_pairs)} 条。分类与诊断筛选仅作用于已整理结果，未整理的来源尚无法分类。</div>}
    <div className="flex flex-wrap items-center gap-3"><span className="text-sm text-muted-foreground">结果筛选</span><Select value={supportIntent} onValueChange={value => setSupportIntent(value as NonNullable<ApiFilters['support_intent']>)}><SelectTrigger className="w-full sm:w-[205px]" aria-label="支持类型"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部支持类型</SelectItem><SelectItem value="configuration_guidance">场景配置与使用指导</SelectItem><SelectItem value="functional_anomaly">功能异常与排障</SelectItem><SelectItem value="unclear">待识别背景</SelectItem></SelectContent></Select><Select value={diagnosisStatus} onValueChange={value => setDiagnosisStatus(value as NonNullable<ApiFilters['diagnosis_status']>)}><SelectTrigger className="w-full sm:w-[180px]" aria-label="诊断状态"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部诊断状态</SelectItem>{(Object.entries(diagnosisLabels) as [DiagnosisStatus, string][]).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></div>
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{[
      ['高频问题', summary.data?.topic_count, '已归并的商家咨询主题'], ['会话验证答案', summary.data?.auto_reusable_knowledge, `已有来源知识 ${count(summary.data?.ready_knowledge)} 条`], ['待定位/来源缺口', summary.data?.exception_count, '待定位、冲突或来源不足'], ['咨询片段', summary.data?.total_events, '已整理内容中的咨询片段'],
    ].map(([title, value, description]) => <Card key={String(title)} className="gap-2"><CardHeader className="pb-1"><CardDescription>{title}</CardDescription><CardTitle className="text-2xl tabular-nums">{count(value as number | undefined)}</CardTitle></CardHeader><CardContent className="text-xs text-muted-foreground">{description}</CardContent></Card>)}</div>
    {summary.error && <QueryPanel hasData={summary.data !== null} loading={false} error={summary.error} retry={summary.retry}>{null}</QueryPanel>}
    {summary.data && !summary.data.semantics.ready && <div role="status" className="flex items-start gap-2 rounded-lg border bg-muted/30 p-4 text-sm text-muted-foreground"><CircleAlert className="mt-0.5 size-4 shrink-0" /><span>语义整理暂不可用{summary.data.semantics.error ? `：${summary.data.semantics.error}` : '，请检查模型网关配置。'}</span></div>}
    {job && <Card className="gap-3"><CardHeader><div className="flex flex-wrap items-center justify-between gap-2"><CardTitle className="text-sm">最近整理任务 <Badge variant="outline" className="ml-2 font-normal">{labels[job.status] || job.status}</Badge></CardTitle><span className="text-xs text-muted-foreground">任务 #{job.id}</span></div><CardDescription>任务范围以提交时的筛选条件为准。</CardDescription></CardHeader><CardContent><div className="flex flex-wrap gap-x-6 gap-y-2 text-sm tabular-nums"><span>总数 {count(job.total)}</span><span>已尝试 {count(job.processed + job.failed + job.skipped)}</span><span>成功 {count(job.processed)}</span><span>失败 {count(job.failed)}</span><span>跳过 {count(job.skipped)}</span></div>{job.error && <p role="alert" className="mt-2 text-sm text-destructive">{job.error}</p>}</CardContent></Card>}
    <Tabs value={tab} onValueChange={setTab} className="gap-4"><TabsList variant="line" className="w-full justify-start gap-5 border-b"><TabsTrigger value="topics">高频问题</TabsTrigger><TabsTrigger value="knowledge">知识与答案</TabsTrigger><TabsTrigger value="exceptions">已知问题与来源缺口</TabsTrigger></TabsList>
      <TabsContent value="topics"><Card><CardHeader><CardTitle>高频问题</CardTitle><CardDescription>按商家咨询次数汇总；咨询片段可包含同一次咨询的多次提及。</CardDescription></CardHeader><CardContent><QueryPanel hasData={topics.data !== null} loading={topics.loading} error={topics.error} retry={topics.retry}>{topics.data?.items.length ? <div className="divide-y">{topics.data.items.map(item => <button key={item.id} onClick={() => { setDetailFilters(null); setSelectedId(item.id) }} className="flex w-full flex-wrap items-center justify-between gap-3 py-4 text-left hover:bg-muted/30"><span className="min-w-0"><span className="block font-medium">{item.theme.trim() || item.question}</span><span className="mt-1 block text-xs text-muted-foreground">代表问法：{item.question}</span><span className="mt-1 block text-xs text-muted-foreground">{intentLabels[item.primary_intent || "unclear"]} · 最近 {item.last_seen || '时间未知'}</span></span><span className="flex gap-4 text-xs tabular-nums text-muted-foreground"><span>{count(item.consultation_count)} 次咨询</span><span>{count(item.event_count)} 个咨询片段</span><span>{count(item.customer_count)} 客户</span><span>{count(item.session_count)} 会话</span><ArrowUpRight className="size-4" /></span></button>)}</div> : <p className="py-10 text-center text-sm text-muted-foreground">当前范围暂无高频问题。</p>}</QueryPanel></CardContent></Card></TabsContent>
      <TabsContent value="knowledge"><Card><CardHeader><div className="flex flex-wrap items-start justify-between gap-3"><div className="space-y-1.5"><CardTitle>知识与答案</CardTitle><CardDescription>完整解答与客户后续反馈共同验证；证据充分的自动可用，无需逐条审批。</CardDescription></div><Select value={knowledgeView} onValueChange={setKnowledgeView}><SelectTrigger className="w-44"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="verified">会话验证通过</SelectItem><SelectItem value="all">全部来源知识</SelectItem></SelectContent></Select></div></CardHeader><CardContent><QueryPanel hasData={knowledge.data !== null} loading={knowledge.loading} error={knowledge.error} retry={knowledge.retry}>{knowledge.data?.items.some(item => knowledgeView === 'all' || item.conversation_verification?.auto_reusable) ? <div className="grid gap-4 lg:grid-cols-2">{knowledge.data.items.filter(item => knowledgeView === 'all' || item.conversation_verification?.auto_reusable).map(item => <div key={item.id} className="rounded-lg border p-4"><KnowledgeContent item={item} onOpenPair={onOpenPair} /></div>)}</div> : <div className="space-y-3 py-10 text-center text-sm text-muted-foreground"><p>{knowledgeView === 'verified' ? '当前范围尚无足够会话闭环证据支持的直接复用答案。' : '当前范围暂无有来源的知识。'}</p>{knowledgeView === 'verified' && Boolean(knowledge.data?.items.length) && <Button variant="outline" size="sm" onClick={() => setKnowledgeView('all')}>查看已有来源知识（{count(knowledge.data?.items.length)}）</Button>}</div>}</QueryPanel></CardContent></Card></TabsContent>
      <TabsContent value="exceptions"><Card><CardHeader><CardTitle>已知问题与来源缺口</CardTitle><CardDescription>查看待定位问题、回复冲突及需要补充的来源。</CardDescription></CardHeader><CardContent><QueryPanel hasData={exceptions.data !== null} loading={exceptions.loading} error={exceptions.error} retry={exceptions.retry}>{exceptions.data?.items.length ? <div className="space-y-3">{exceptions.data.items.map(item => <div key={item.id} className="space-y-2 rounded-lg border p-4"><div className="flex flex-wrap items-center gap-2"><Badge variant="outline">{exceptionLabels[item.kind] || "其他待核对"}</Badge><span className="font-medium">{item.question}</span></div><p className="whitespace-pre-wrap text-sm text-muted-foreground">{item.reason}</p>{item.topic_id && <Button variant="outline" size="sm" onClick={() => { setDetailFilters(null); setSelectedId(item.topic_id!) }}>查看问题依据 <ArrowUpRight /></Button>}</div>)}</div> : <p className="py-10 text-center text-sm text-muted-foreground">当前范围没有其他需要补充证据或处理冲突的记录。</p>}</QueryPanel><div className="mt-6 border-t pt-6"><h3 className="font-medium">已确认缺陷</h3><p className="mt-1 text-sm text-muted-foreground">仅展示有明确确认依据的缺陷；商家报告或待定位问题不会列为已确认缺陷。</p><QueryPanel hasData={defects.data !== null} loading={defects.loading} error={defects.error} retry={defects.retry}>{defects.data?.items.length ? <div className="mt-4"><EvidenceList evidence={defects.data.items} onOpenPair={onOpenPair} /></div> : <p className="py-8 text-center text-sm text-muted-foreground">当前范围暂无已确认缺陷。</p>}</QueryPanel></div></CardContent></Card></TabsContent>
    </Tabs>
    <Sheet open={selectedId !== null} onOpenChange={open => { if (!open) { setSelectedId(null); setDetailFilters(null) } }}><SheetContent className="w-full overflow-y-auto sm:max-w-2xl"><SheetHeader><SheetTitle>问题与原句依据</SheetTitle><SheetDescription>按对应查询范围展示相关会话。</SheetDescription></SheetHeader><div className="space-y-5 px-4 pb-6"><QueryPanel hasData={detail.data !== null} loading={detail.loading} error={detail.error} retry={detail.retry}>{detail.data && <><div><h3 className="text-lg font-semibold">{detail.data.topic.theme.trim() || detail.data.topic.question}</h3><p className="mt-2 text-sm text-muted-foreground">代表问法：{detail.data.topic.question}</p><p className="mt-2 text-sm text-muted-foreground">{intentLabels[detail.data.topic.primary_intent || "unclear"]}</p><p className="mt-3 text-sm tabular-nums">{count(detail.data.topic.consultation_count)} 次咨询 · {count(detail.data.topic.event_count)} 个咨询片段 · {count(detail.data.topic.customer_count)} 客户 · {count(detail.data.topic.session_count)} 会话</p></div><div><h4 className="mb-3 font-medium">原句依据</h4><EvidenceList evidence={detail.data.events} onOpenPair={onOpenPair} /></div></>}</QueryPanel></div></SheetContent></Sheet>
    <Sheet open={selectedEvent !== null || selectedKnowledge !== null} onOpenChange={open => { if (!open) { setSelectedEvent(null); setSelectedKnowledge(null); setDetailFilters(null) } }}><SheetContent className="w-full overflow-y-auto sm:max-w-2xl"><SheetHeader><SheetTitle>{selectedEvent ? '来源事件' : '知识依据'}</SheetTitle><SheetDescription>查看回答所引用的来源与原句。</SheetDescription></SheetHeader><div className="space-y-5 px-4 pb-6">{selectedEvent && <QueryPanel hasData={eventDetail.data !== null} loading={eventDetail.loading} error={eventDetail.error} retry={eventDetail.retry}>{eventDetail.data && <><EvidenceList evidence={[eventDetail.data.event]} onOpenPair={onOpenPair} />{eventDetail.data.topic && <div className="text-sm text-muted-foreground">所属问题：{eventDetail.data.topic.question}</div>}</>}</QueryPanel>}{selectedKnowledge && <QueryPanel hasData={knowledgeDetail.data !== null} loading={knowledgeDetail.loading} error={knowledgeDetail.error} retry={knowledgeDetail.retry}>{knowledgeDetail.data && <KnowledgeContent item={knowledgeDetail.data.item} onOpenPair={onOpenPair} />}</QueryPanel>}</div></SheetContent></Sheet>
    <Sheet open={chatOpen} onOpenChange={setChatOpen}><SheetContent className="w-full sm:max-w-xl"><SheetHeader><SheetTitle className="flex items-center gap-2"><Bot className="size-5" />问问数据</SheetTitle><SheetDescription>基于当前筛选范围查询会话结果，并查看回答依据。</SheetDescription></SheetHeader><div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4">{messages.length === 0 && <div className="rounded-lg border bg-muted/20 p-5 text-sm text-muted-foreground"><p className="font-medium text-foreground">从一个问题开始</p><p className="mt-2">可以询问当前范围的高频诉求、客服答复或待补充的信息。</p></div>}{messages.map((message, index) => <div key={index} className={message.role === 'user' ? 'ml-8 rounded-xl bg-primary p-3 text-sm text-primary-foreground' : 'mr-3 rounded-xl border bg-muted/20 p-3 text-sm'}><p className="whitespace-pre-wrap break-words">{message.content}</p><AnswerContext message={message} />{message.citations && message.citations.length > 0 && <div className="mt-3 space-y-1 border-t pt-2"><div className="text-xs text-muted-foreground">回答依据</div>{message.citations.map((citation, i) => <button key={i} type="button" onClick={() => openCitation(citation, message)} className="flex w-full items-center gap-2 rounded px-1 py-1 text-left text-xs hover:bg-muted"><FileText className="size-3.5" />{citation.title || `来源 #${citation.id}`}<ArrowUpRight className="ml-auto size-3.5" /></button>)}</div>}</div>)}{asking && <div role="status" className="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在查询…</div>}{chatError && <p role="alert" className="text-sm text-destructive">{chatError}</p>}</div><form onSubmit={ask} className="flex gap-2 border-t p-4"><Input aria-label="向数据提问" value={question} onChange={event => setQuestion(event.target.value)} placeholder="输入你想了解的问题" maxLength={2000} disabled={asking} /><Button type="submit" size="icon" disabled={!question.trim() || asking} aria-label="发送问题"><Send /></Button></form></SheetContent></Sheet>
    <Dialog open={runOpen} onOpenChange={setRunOpen}><DialogContent><DialogHeader><DialogTitle>整理当前范围</DialogTitle><DialogDescription>按照顶部日期、客户群和客服筛选创建整理任务。尚未整理的来源无法分类，结果类型和诊断筛选不限制本次整理范围。</DialogDescription></DialogHeader><label className="space-y-2 text-sm"><span>最多整理问答数（1–100）</span><Input type="number" min="1" max="100" value={limit} onChange={event => setLimit(event.target.value)} /></label><DialogFooter><Button variant="outline" onClick={() => setRunOpen(false)}>取消</Button><Button onClick={run} disabled={running}>{running && <LoaderCircle className="animate-spin" />}开始整理</Button></DialogFooter></DialogContent></Dialog>
  </div>
}

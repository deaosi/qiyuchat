import { useEffect, useRef, useState } from 'react'
import {
  BrainCircuit, Check, CheckCheck, ChevronLeft, ChevronRight,
  CircleCheck, Clock3, EyeOff, FileText, GitMerge, History, Layers3, LoaderCircle,
  MessageSquareText, Plus, RefreshCw, Scissors, Search, Sparkles, TriangleAlert, X,
} from 'lucide-react'
import { toast } from 'sonner'
import { LoadingState, LoadingValue } from './loading-state'
import { useApi } from '@/hooks/use-api'
import {
  ApiError, apiPost, normalizePair, type ApiFilters, type VocDetail, type VocEvidence,
  type VocJob, type VocStatus, type VocSummary, type VocTopic, type VocTopicList,
} from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'
import { customerLabel } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'

interface Props {
  filters: ApiFilters
  refreshKey?: number
  onOpenPair: (pair: QAPair) => void
  onRuleClusters: () => void
  onCreateKnowledge?: (topic: VocTopic) => void
}
interface Draft { question: string; theme: string; boundary: string }
const emptyDraft: Draft = { question: '', theme: '', boundary: '' }
const topicLabels: Record<VocStatus, string> = { pending: '待复核', confirmed: '已确认', ignored: '已忽略' }
const jobLabels: Record<VocJob['status'], string> = { queued: '排队中', running: '正在整理', completed: '已完成', partial: '部分完成', failed: '整理失败' }
const number = (value: number | undefined) => value === undefined ? '—' : value.toLocaleString('zh-CN')
const isActive = (job: VocJob | null | undefined) => job?.status === 'queued' || job?.status === 'running'

function TopicStatus({ status }: { status: VocStatus }) {
  const Icon = status === 'confirmed' ? CircleCheck : status === 'ignored' ? EyeOff : Clock3
  return <Badge variant="outline" className="gap-1.5 font-normal"><Icon className="size-3.5" />{topicLabels[status]}</Badge>
}

function ErrorState({ error, retry }: { error: string | null; retry: () => void }) {
  return <div className="flex flex-col items-center gap-3 px-5 py-10 text-center text-sm" role="alert">
    <TriangleAlert className="size-5 text-muted-foreground" /><p className="max-w-lg whitespace-pre-wrap break-words text-muted-foreground">{error || '读取失败，请稍后重试。'}</p><Button variant="outline" size="sm" onClick={retry}><RefreshCw />重试</Button>
  </div>
}



function DraftFields({ draft, onChange, disabled, prefix }: { draft: Draft; onChange: (next: Draft) => void; disabled: boolean; prefix: string }) {
  return <div className="grid gap-4">
    <div className="space-y-2"><Label htmlFor={`${prefix}-question`}>标准问题<span className="text-muted-foreground">（必填，3–120 字）</span></Label><Input id={`${prefix}-question`} value={draft.question} maxLength={120} disabled={disabled} onChange={event => onChange({ ...draft, question: event.target.value })} placeholder="用一句完整问句表达同一类客户诉求" /></div>
    <div className="space-y-2"><Label htmlFor={`${prefix}-theme`}>业务主题<span className="text-muted-foreground">（最多 60 字）</span></Label><Input id={`${prefix}-theme`} value={draft.theme} maxLength={60} disabled={disabled} onChange={event => onChange({ ...draft, theme: event.target.value })} placeholder="例如：物流履约 / 发货时效" /></div>
    <div className="space-y-2"><Label htmlFor={`${prefix}-boundary`}>归并范围说明<span className="text-muted-foreground">（最多 500 字）</span></Label><textarea id={`${prefix}-boundary`} value={draft.boundary} maxLength={500} disabled={disabled} onChange={event => onChange({ ...draft, boundary: event.target.value })} rows={3} placeholder="说明包含哪些问法，以及哪些相近问题应单独归类" className="flex min-h-24 w-full rounded-md border bg-background px-3 py-2 text-sm shadow-xs outline-none transition-[color,box-shadow] placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50" /></div>
  </div>
}

export function VocWorkbench({ filters, refreshKey = 0, onOpenPair, onRuleClusters, onCreateKnowledge }: Props) {
  const [revision, setRevision] = useState(0)
  const [pollRevision, setPollRevision] = useState(0)
  const [summarySnapshot, setSummarySnapshot] = useState<{ key: string; data: VocSummary } | null>(null)
  const [tab, setTab] = useState<VocStatus | 'new_evidence'>('pending')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(0)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detailRevision, setDetailRevision] = useState(0)
  const [topicSnapshot, setTopicSnapshot] = useState<{ key: string; topic: VocTopic } | null>(null)
  const [draftRevision, setDraftRevision] = useState<number | null>(null)
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const [selectedEvidence, setSelectedEvidence] = useState<Set<number>>(new Set())
  const [evidenceStatus, setEvidenceStatus] = useState<'all' | 'pending' | 'confirmed'>('all')
  const [evidencePage, setEvidencePage] = useState(0)
  const [busy, setBusy] = useState<string | null>(null)
  const [runOpen, setRunOpen] = useState(false)
  const [runLimit, setRunLimit] = useState('500')
  const [jobsOpen, setJobsOpen] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [createDraft, setCreateDraft] = useState<Draft>(emptyDraft)
  const [splitOpen, setSplitOpen] = useState(false)
  const [splitDraft, setSplitDraft] = useState<Draft>(emptyDraft)
  const [mergeOpen, setMergeOpen] = useState(false)
  const [mergeTarget, setMergeTarget] = useState('')
  const [mergeSearch, setMergeSearch] = useState('')
  const [ignoreOpen, setIgnoreOpen] = useState(false)
  const [excludeOpen, setExcludeOpen] = useState(false)
  const [refreshTopicOpen, setRefreshTopicOpen] = useState(false)
  const [submittedJob, setSubmittedJob] = useState<VocJob | null>(null)
  const scopeKey = JSON.stringify(filters)
  const scopeRef = useRef(scopeKey)
  const mounted = useRef(true)
  const activeJobId = useRef<number | null>(null)
  const draftKey = useRef('')
  const forceDraftReload = useRef(false)
  scopeRef.current = scopeKey
  const refresh = refreshKey + revision
  const summary = useApi<VocSummary>('voc/summary', filters, refresh + pollRevision)
  const summaryData = summary.data ?? (summarySnapshot?.key === scopeKey ? summarySnapshot.data : null)
  const summaryInitialLoading = summary.loading && !summaryData
  const topics = useApi<VocTopicList>('voc/topics', { ...filters, status: tab === 'new_evidence' ? 'all' : tab, topic_q: query }, refresh)
  const detailScopeKey = `${scopeKey}:${selectedId ?? ''}`
  const detail = useApi<VocDetail>('voc/detail', { ...filters, id: selectedId ?? 0, limit: 8, offset: evidencePage * 8, evidence_status: evidenceStatus }, detailRevision + refreshKey, selectedId !== null)
  const history = useApi<{ items: VocJob[] }>('voc/jobs', { source: filters.source }, refresh, jobsOpen)
  const mergeTopics = useApi<VocTopicList>('voc/topics', { source: filters.source, status: 'all' }, refresh, mergeOpen)
  const latestJob = summaryData?.latest_job && (!submittedJob || summaryData.latest_job.id >= submittedJob.id) ? summaryData.latest_job : submittedJob || summaryData?.latest_job || null
  const running = isActive(latestJob)
  const topic = detail.data?.topic ?? (topicSnapshot?.key === detailScopeKey ? topicSnapshot.topic : null)
  const dirty = Boolean(topic && (draft.question !== topic.question || draft.theme !== topic.theme || draft.boundary !== topic.boundary))
  const staleVersion = Boolean(topic && draftRevision !== null && topic.revision !== draftRevision)
  const reviewBusy = Boolean(busy) || detail.loading || staleVersion
  const evidenceTotal = detail.data?.total ?? 0
  const evidencePages = Math.max(1, Math.ceil(evidenceTotal / 8))
  const currentEvidencePage = evidencePage
  const visibleEvidence = detail.data?.evidence || []
  const selectedOnPage = visibleEvidence.filter(item => selectedEvidence.has(item.qa_id)).length
  const target = mergeTopics.data?.items.find(item => String(item.id) === mergeTarget)
  const candidates = (mergeTopics.data?.items || []).filter(item => item.id !== selectedId && item.status !== 'ignored' && `${item.question} ${item.theme}`.toLocaleLowerCase().includes(mergeSearch.trim().toLocaleLowerCase()))
  const topicItems = (topics.data?.items || []).filter(item => tab !== 'new_evidence' || (item.status !== 'ignored' && item.pending_count > 0))
  const topicPages = Math.max(1, Math.ceil(topicItems.length / 10))
  const currentPage = Math.min(page, topicPages - 1)
  const topicRows = topicItems.slice(currentPage * 10, (currentPage + 1) * 10)
  const scopeLabel = filters.start || filters.end ? `${filters.start || '不限起始'} 至 ${filters.end || '不限结束'}` : filters.days ? `近 ${filters.days} 天` : '全部时间'

  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => { if (summary.data) setSummarySnapshot({ key: scopeKey, data: summary.data }) }, [scopeKey, summary.data])
  useEffect(() => { const timer = window.setTimeout(() => setQuery(search.trim()), 280); return () => window.clearTimeout(timer) }, [search])
  useEffect(() => { setPage(0) }, [scopeKey, tab, query])
  useEffect(() => { setSelectedId(null); setSelectedEvidence(new Set()); setSubmittedJob(null); activeJobId.current = null }, [filters.source])
  useEffect(() => { setSelectedId(null); setSelectedEvidence(new Set()); setEvidencePage(0) }, [scopeKey])
  useEffect(() => {
    if (!detail.data) return
    const value = detail.data.topic
    setTopicSnapshot({ key: detailScopeKey, topic: value })
    if (draftKey.current !== detailScopeKey || forceDraftReload.current) {
      setDraft({ question: value.question, theme: value.theme, boundary: value.boundary })
      setDraftRevision(value.revision)
      draftKey.current = detailScopeKey
      forceDraftReload.current = false
    }
    const lastPage = Math.max(0, Math.ceil(detail.data.total / 8) - 1)
    if (evidencePage > lastPage) setEvidencePage(lastPage)
  }, [detail.data, detailScopeKey, evidencePage])
  useEffect(() => { setEvidencePage(0) }, [detailScopeKey, evidenceStatus])
  useEffect(() => { setSelectedEvidence(new Set()) }, [detailScopeKey, evidenceStatus, evidencePage])
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setPollRevision(value => value + 1), 2500)
    return () => window.clearInterval(timer)
  }, [running, filters.source])
  useEffect(() => {
    if (!latestJob) return
    if (isActive(latestJob)) { activeJobId.current = latestJob.id; return }
    if (activeJobId.current !== latestJob.id) return
    activeJobId.current = null
    setRevision(value => value + 1)
    if (latestJob.status === 'completed') toast.success(`AI 整理完成，已处理 ${latestJob.processed} 条问答。请复核标准问题和原句。`)
    else if (latestJob.status === 'partial') toast.warning(`部分整理完成，${latestJob.failed} 条处理失败。可继续整理当前范围。`)
    else toast.error(latestJob.error || 'AI 整理失败，请查看任务记录后重试。')
  }, [latestJob])

  function reload(includeDetail = false) { setRevision(value => value + 1); if (includeDetail) setDetailRevision(value => value + 1) }
  function openTopic(item: VocTopic) { setSelectedId(item.id); setSelectedEvidence(new Set()); setEvidenceStatus(tab === 'new_evidence' ? 'pending' : 'all'); setEvidencePage(0) }
  function requireQuestion(value: Draft) {
    if ([...value.question.trim()].length < 3 || [...value.question.trim()].length > 120) { toast.error('标准问题须为 3–120 字。'); return false }
    if ([...value.theme.trim()].length > 60) { toast.error('业务主题不能超过 60 字。'); return false }
    if ([...value.boundary.trim()].length > 500) { toast.error('归并范围说明不能超过 500 字。'); return false }
    return true
  }
  function draftBody(value: Draft) { return { question: value.question.trim(), theme: value.theme.trim(), boundary: value.boundary.trim() } }
  function acceptTopic(value: VocTopic) {
    const key = `${scopeKey}:${value.id}`
    setTopicSnapshot({ key, topic: value }); draftKey.current = key
    setDraft({ question: value.question, theme: value.theme, boundary: value.boundary }); setDraftRevision(value.revision)
    forceDraftReload.current = false
  }
  function readLatestTopic() { forceDraftReload.current = true; setRefreshTopicOpen(false); detail.retry() }
  async function mutate<T>(name: string, body: Record<string, unknown>, label: string, success: (result: T) => void) {
    if (busy) return
    const originalScope = scopeKey
    setBusy(name)
    try {
      const result = await apiPost<T>(name, filters, JSON.stringify(body), { 'Content-Type': 'application/json' })
      if (!mounted.current || scopeRef.current !== originalScope) return
      success(result)
      reload(true)
      toast.success(label)
    } catch (error) {
      if (!mounted.current || scopeRef.current !== originalScope) return
      if (error instanceof ApiError && error.status === 401) return
      if (error instanceof ApiError && error.status === 409) {
        forceDraftReload.current = true
        setSelectedEvidence(new Set()); setSplitOpen(false); setExcludeOpen(false); setIgnoreOpen(false); setMergeOpen(false)
        reload(true); mergeTopics.retry(); setMergeTarget('')
        toast.warning('该问题已发生变更，已刷新最新版本。请核对后重新操作。')
      } else toast.error(error instanceof Error ? error.message : '操作失败，请稍后重试。')
    } finally { if (mounted.current) setBusy(null) }
  }
  function saveTopic(status: VocStatus) {
    if (!topic || !requireQuestion(draft)) return
    if (staleVersion) { toast.warning('问题版本已更新，请先读取最新版本。'); return }
    void mutate<{ topic: VocTopic }>('voc/topic', { id: topic.id, ...draftBody(draft), status, revision: draftRevision ?? topic.revision }, status === 'confirmed' ? '已确认问题及当前范围原句。' : status === 'ignored' ? '已移到已忽略，原始问答仍保留。' : '标准问题已保存。', result => { acceptTopic(result.topic); setIgnoreOpen(false) })
  }
  function updateEvidence(action: 'exclude' | 'confirm' | 'split') {
    if (!topic || !selectedEvidence.size) return
    if (dirty) { toast.info('请先保存标准问题编辑，再调整原句。'); return }
    if (staleVersion) { toast.warning('问题版本已更新，请先读取最新版本。'); return }
    if (action === 'split' && !requireQuestion(splitDraft)) return
    void mutate<{ topic: VocTopic }>('voc/evidence', { topic_id: topic.id, qa_ids: [...selectedEvidence], action, revision: draftRevision ?? topic.revision, ...(action === 'split' ? { new_question: splitDraft.question.trim(), new_theme: splitDraft.theme.trim() } : {}) }, action === 'exclude' ? '已从此问题排除所选原句，原始问答未删除。' : action === 'confirm' ? '所选原句已确认。' : '所选原句已拆分到新问题。', result => { acceptTopic(result.topic); setSelectedEvidence(new Set()); setExcludeOpen(false); setSplitOpen(false); if (action === 'split') setSelectedId(result.topic.id) })
  }
  function selectEvidence(id: number, checked: boolean) { setSelectedEvidence(current => { const next = new Set(current); checked ? next.add(id) : next.delete(id); return next }) }
  function selectEvidencePage(checked: boolean) { setSelectedEvidence(current => { const next = new Set(current); visibleEvidence.forEach(item => checked ? next.add(item.qa_id) : next.delete(item.qa_id)); return next }) }
  function run() {
    const limit = Number(runLimit)
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) { toast.error('单次整理条数须为 1–500 的整数。'); return }
    void mutate<{ job: VocJob }>('voc/run', { limit }, '整理任务已提交，可继续浏览和审核问题。', result => { setSubmittedJob(result.job); setRunOpen(false) })
  }
  const done = latestJob ? latestJob.processed + latestJob.failed + latestJob.skipped : 0

  return <div className="min-w-0 space-y-6">
    <div className="flex flex-wrap items-start justify-between gap-4"><div><h2 className="text-lg font-semibold tracking-tight">从真实会话整理标准问题</h2><p className="mt-1 text-sm text-muted-foreground">AI 提出归并建议，结合原句证据复核，持续补充新的客户诉求。</p><p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground"><Sparkles className="size-3.5" />{summaryInitialLoading ? '正在核验整理模型…' : summaryData?.configured ? `整理模型 · ${summaryData.model}` : '整理模型尚未配置'} </p></div><div className="flex flex-wrap gap-2"><Button variant="outline" size="sm" onClick={onRuleClusters}><Layers3 />规则问题簇</Button><Button variant="outline" size="sm" onClick={() => setJobsOpen(true)}><History />整理记录</Button><Button variant="outline" size="sm" onClick={() => { setCreateDraft(emptyDraft); setCreateOpen(true) }} disabled={Boolean(busy)}><Plus />手动新增</Button><Button size="sm" onClick={() => setRunOpen(true)} disabled={Boolean(busy) || running || !summaryData?.configured || summaryInitialLoading || (summaryData.pending_pairs === 0 && !['failed', 'partial'].includes(latestJob?.status || ''))}>{running ? <LoaderCircle className="animate-spin" /> : <BrainCircuit />}{running ? 'AI 正在整理' : latestJob?.status === 'failed' || latestJob?.status === 'partial' ? '继续整理当前范围' : 'AI 整理当前范围'}</Button></div></div>
    {summary.error ? <Card><ErrorState error={summary.error} retry={summary.retry} /></Card> : <div className="grid gap-4 md:grid-cols-3">
      <Card><CardHeader><CardDescription>标准问题主题</CardDescription><CardTitle className="text-3xl font-semibold tabular-nums">{summaryInitialLoading ? <LoadingValue /> : number(summaryData?.topic_count)}</CardTitle><CardAction><Layers3 className="size-4 text-muted-foreground" /></CardAction></CardHeader><CardFooter className="text-xs text-muted-foreground">已确认 {number(summaryData?.confirmed_topics)} 个 · 已忽略 {number(summaryData?.ignored_topics)} 个</CardFooter></Card>
      <Card><CardHeader><CardDescription>待复核问题</CardDescription><CardTitle className="text-3xl font-semibold tabular-nums">{summaryInitialLoading ? <LoadingValue /> : number(summaryData?.pending_topics)}</CardTitle><CardAction><ClipboardIcon /></CardAction></CardHeader><CardFooter className="text-xs text-muted-foreground">核对标准问法、归并范围及对应原句。</CardFooter></Card>
      <Card><CardHeader><CardDescription>待整理新增问答</CardDescription><CardTitle className="text-3xl font-semibold tabular-nums">{summaryInitialLoading ? <LoadingValue /> : number(summaryData?.pending_pairs)}</CardTitle><CardAction><MessageSquareText className="size-4 text-muted-foreground" /></CardAction></CardHeader><CardFooter className="text-xs text-muted-foreground">当前范围已处理 {number(summaryData?.processed_pairs)} / {number(summaryData?.total_pairs)} 条问答</CardFooter></Card>
    </div>}
    {latestJob && <Card className="gap-4 py-5"><CardHeader><div className="flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2"><CardTitle className="text-sm">{running ? 'AI 整理进度' : '最近一次整理'}</CardTitle><Badge variant="outline" className="gap-1.5 font-normal">{running && <LoaderCircle className="size-3 animate-spin" />}{jobLabels[latestJob.status]}</Badge></div><span className="text-xs text-muted-foreground">任务 #{latestJob.id} · {latestJob.created_at || '时间未知'}</span></div><CardDescription className="text-xs">本任务按提交时范围统计；修改当前筛选不会改变任务范围。</CardDescription></CardHeader><CardContent className="space-y-3"><Progress value={latestJob.total ? Math.min(100, done / latestJob.total * 100) : 0} aria-label={`已处理 ${done} / ${latestJob.total} 条`} className="h-1.5" /><div className="flex flex-wrap justify-between gap-x-4 gap-y-2 text-xs text-muted-foreground"><span>已处理 {number(done)} / {number(latestJob.total)}</span><span>成功覆盖 {number(latestJob.processed)} · 失败 {number(latestJob.failed)} · 跳过 {number(latestJob.skipped)}</span></div>{latestJob.error && <p className="whitespace-pre-wrap text-sm text-destructive [overflow-wrap:anywhere]" role="alert">{latestJob.error}</p>}{(latestJob.status === 'failed' || latestJob.status === 'partial') && <p className="text-xs text-muted-foreground">已完成的结果可继续复核。再次整理当前范围时，系统会跳过已处理的问答。</p>}</CardContent></Card>}
    <Card className="min-w-0 gap-4"><CardHeader><div className="flex flex-wrap items-center justify-between gap-3"><Tabs value={tab} onValueChange={value => setTab(value as VocStatus | 'new_evidence')}><TabsList><TabsTrigger value="pending">待复核</TabsTrigger><TabsTrigger value="new_evidence">新增待核对</TabsTrigger><TabsTrigger value="confirmed">已确认</TabsTrigger><TabsTrigger value="ignored">已忽略</TabsTrigger></TabsList></Tabs><Button variant="ghost" size="sm" onClick={() => reload()} disabled={topics.loading}><RefreshCw className={topics.loading ? 'animate-spin' : ''} />刷新</Button></div><div className="relative mt-2 max-w-md"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input value={search} maxLength={200} onChange={event => setSearch(event.target.value)} placeholder="搜索标准问题或业务主题…" aria-label="搜索标准问题" className="pl-9 pr-9" />{search && <Button size="icon-xs" variant="ghost" className="absolute right-1.5 top-1/2 -translate-y-1/2" onClick={() => setSearch('')} aria-label="清空标准问题搜索"><X /></Button>}</div>{tab === 'new_evidence' && <p className="mt-1 text-xs leading-5 text-muted-foreground">集中核对当前范围的待审原句，包含已确认主题后续新增的问法；原主题的确认状态继续保留。</p>}</CardHeader><CardContent className="min-w-0">
      {topics.loading ? <LoadingState label="正在读取标准问题…" /> : topics.error ? <ErrorState error={topics.error} retry={topics.retry} /> : !topicItems.length ? <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-12 text-center"><BrainCircuit className="size-7 text-muted-foreground/60" /><h3 className="text-sm font-medium">{query ? '没有匹配的标准问题' : tab === 'pending' ? '当前范围没有待复核问题' : tab === 'new_evidence' ? '当前范围没有新增待核对原句' : tab === 'confirmed' ? '当前范围还没有已确认问题' : '当前范围没有已忽略问题'}</h3><p className="max-w-lg text-sm leading-6 text-muted-foreground">{query ? '可以调整标准问题关键词，或放宽上方日期、客户群和客服筛选。' : '点击“AI 整理当前范围”，从会话文字生成标准问题与原句证据；也可以手动新增，再逐条复核。'}</p>{query && <Button variant="outline" size="sm" onClick={() => setSearch('')}>清空搜索</Button>}</div> : <div className="overflow-hidden rounded-lg border"><Table className="table-fixed min-w-[920px]"><TableHeader><TableRow><TableHead>标准问题</TableHead><TableHead className="w-36">业务主题</TableHead><TableHead className="w-24 text-right">涉及问答</TableHead><TableHead className="w-20 text-right">客户数</TableHead><TableHead className="w-24 text-right">待核对</TableHead><TableHead className="w-32">最近出现</TableHead><TableHead className="w-28"><span className="sr-only">操作</span></TableHead></TableRow></TableHeader><TableBody>{topicRows.map(item => <TableRow key={item.id}><TableCell className="min-w-0 whitespace-normal py-4 align-top"><button className="block w-full whitespace-normal text-left text-sm font-medium leading-6 [overflow-wrap:anywhere] hover:underline" onClick={() => openTopic(item)}>{item.question}</button><p className="mt-1.5 text-xs text-muted-foreground">标准问题 #{item.id} · {item.count ? '点击核对原句证据' : '尚未关联原句'}{tab === 'new_evidence' && <span className="ml-2">{item.status === 'confirmed' ? '已确认主题 · 有新增原句' : '主题待复核'}</span>}</p></TableCell><TableCell className="whitespace-normal py-4 align-top text-muted-foreground [overflow-wrap:anywhere]">{item.theme || '未分类'}</TableCell><TableCell className="py-4 text-right align-top tabular-nums">{number(item.count)}</TableCell><TableCell className="py-4 text-right align-top tabular-nums">{number(item.customers)}</TableCell><TableCell className="py-4 text-right align-top">{item.pending_count ? <Badge variant="secondary" className="tabular-nums">{number(item.pending_count)}</Badge> : <span className="text-muted-foreground">—</span>}</TableCell><TableCell className="whitespace-normal py-4 align-top text-xs text-muted-foreground">{item.last_seen || '时间未知'}</TableCell><TableCell className="py-3 text-right align-top"><Button type="button" size="sm" variant="outline" onClick={() => openTopic(item)} aria-label={`查看标准问题 ${item.id} 的详情`}>查看详情</Button></TableCell></TableRow>)}</TableBody></Table></div>}
    </CardContent><CardFooter className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground"><p className="max-w-xl leading-5">涉及问答包含待核对原句，按主题内问答 ID 去重。不同主题可能重叠，数量不作跨行相加。</p>{topicItems.length > 0 && <div className="flex items-center gap-2"><span>共 {number(tab === 'new_evidence' ? topicItems.length : topics.data?.total)} 个问题 · {currentPage + 1}/{topicPages}</span><Button size="icon-sm" variant="outline" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)} aria-label="上一页标准问题"><ChevronLeft /></Button><Button size="icon-sm" variant="outline" disabled={currentPage >= topicPages - 1} onClick={() => setPage(currentPage + 1)} aria-label="下一页标准问题"><ChevronRight /></Button></div>}</CardFooter></Card>

    <Sheet open={selectedId !== null} onOpenChange={open => { if (!open) setSelectedId(null) }}><SheetContent className="w-full gap-0 sm:max-w-[820px]"><SheetHeader className="shrink-0 border-b p-6 pr-12"><SheetTitle className="flex flex-wrap items-center gap-2 text-lg">标准问题复核{topic && <TopicStatus status={topic.status} />}</SheetTitle><SheetDescription>名称、主题和范围说明全局生效；下方原句按当前筛选范围展示。</SheetDescription></SheetHeader><div className="min-h-0 flex-1 overflow-y-auto px-6 pb-6 [overflow-wrap:anywhere]">{detail.loading && !topic ? <LoadingState label="正在读取标准问题与原句证据…" /> : detail.error && !topic ? <ErrorState error={detail.error} retry={detail.retry} /> : topic && <>
      <div className="flex flex-wrap items-center justify-between gap-2 py-4 text-xs text-muted-foreground"><span>标准问题 #{topic.id} · 编辑版本 {draftRevision ?? topic.revision}</span><Button size="xs" variant="ghost" disabled={Boolean(busy)} onClick={() => dirty ? setRefreshTopicOpen(true) : readLatestTopic()}><RefreshCw />读取最新版本</Button></div>
      <DraftFields draft={draft} onChange={setDraft} disabled={Boolean(busy) || staleVersion} prefix="voc-edit" />
      <div className="mt-5 flex flex-wrap gap-2"><Button variant="outline" size="sm" disabled={reviewBusy || !dirty} onClick={() => saveTopic(topic.status)}>{busy === 'voc/topic' && <LoaderCircle className="animate-spin" />}保存修改</Button>{topic.status !== 'confirmed' && <Button size="sm" disabled={reviewBusy} onClick={() => saveTopic('confirmed')}><CheckCheck />确认并收录当前范围原句</Button>}{topic.status === 'confirmed' && topic.pending_count > 0 && <Button size="sm" disabled={reviewBusy} onClick={() => saveTopic('confirmed')}><CheckCheck />确认当前范围新增原句</Button>}{topic.status !== 'ignored' ? <Button variant="ghost" size="sm" disabled={reviewBusy} onClick={() => setIgnoreOpen(true)}><EyeOff />忽略此问题</Button> : <Button variant="outline" size="sm" disabled={reviewBusy} onClick={() => saveTopic('pending')}>恢复待复核</Button>}<Button variant="ghost" size="sm" disabled={reviewBusy || dirty} onClick={() => { setMergeTarget(''); setMergeSearch(''); setMergeOpen(true) }}><GitMerge />合并到现有问题</Button></div>
      {onCreateKnowledge && <div className="mt-3"><Button variant="outline" size="sm" disabled={reviewBusy || dirty || !topic.count || topic.answered_count === 0 || topic.status === 'ignored'} onClick={() => { setSelectedId(null); onCreateKnowledge(topic) }}><Sparkles />整理成知识</Button><p className="mt-2 text-xs text-muted-foreground">{topic.answered_count === 0 ? '当前范围此问题没有可用客服回复，补充含回答的记录后再整理。' : '结合当前范围的原句与客服实际回复生成知识草稿，审核后再使用。'}</p></div>}
      {dirty && <p className="mt-3 text-xs text-muted-foreground">有未保存的文字编辑。请先保存，再刷新、调整原句或合并问题。</p>}
      {staleVersion && <p className="mt-3 rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">问题版本已有更新，请读取最新版本后继续审核。</p>}
      <Separator className="my-6" />
      <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-sm font-semibold">原句证据 <span className="ml-1.5 font-normal text-muted-foreground">{number(topic.count)} 条</span></h3><p className="mt-1 text-xs text-muted-foreground">已确认 {number(Math.max(0, topic.count - topic.pending_count))} 条 · 待核对 {number(topic.pending_count)} 条</p></div><Select value={evidenceStatus} onValueChange={value => { setEvidenceStatus(value as typeof evidenceStatus); setSelectedEvidence(new Set()); setEvidencePage(0) }}><SelectTrigger size="sm" className="w-36" aria-label="原句审核状态"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部原句</SelectItem><SelectItem value="pending">待核对</SelectItem><SelectItem value="confirmed">已确认</SelectItem></SelectContent></Select></div>
      <div className="my-4 flex flex-wrap items-center gap-2"><span className="mr-1 text-xs text-muted-foreground">已选当前页 {selectedEvidence.size} 条</span><Button variant="outline" size="xs" disabled={!selectedEvidence.size || reviewBusy || dirty} onClick={() => updateEvidence('confirm')}><Check />确认所选</Button><Button variant="outline" size="xs" disabled={!selectedEvidence.size || reviewBusy || dirty} onClick={() => setExcludeOpen(true)}><X />排除所选</Button><Button variant="outline" size="xs" disabled={!selectedEvidence.size || reviewBusy || dirty} onClick={() => { setSplitDraft({ question: '', theme: topic.theme, boundary: '' }); setSplitOpen(true) }}><Scissors />拆为新问题</Button>{selectedEvidence.size > 0 && <Button variant="ghost" size="xs" disabled={Boolean(busy)} onClick={() => setSelectedEvidence(new Set())}>取消选择</Button>}{busy === 'voc/evidence' && <LoaderCircle className="size-4 animate-spin" />}</div>
      {detail.loading ? <LoadingState label="正在读取这页原句…" /> : detail.error ? <ErrorState error={detail.error} retry={detail.retry} /> : visibleEvidence.length ? <div className="overflow-hidden rounded-lg border"><Table className="table-fixed min-w-[630px]"><TableHeader><TableRow><TableHead className="w-10"><Checkbox checked={selectedOnPage === visibleEvidence.length ? true : selectedOnPage ? 'indeterminate' : false} onCheckedChange={checked => selectEvidencePage(Boolean(checked))} disabled={Boolean(busy)} aria-label="选择当前页全部原句" /></TableHead><TableHead>客户原句</TableHead><TableHead className="w-32">来源 / 时间</TableHead><TableHead className="w-24">状态</TableHead><TableHead className="w-16"><span className="sr-only">跳转</span></TableHead></TableRow></TableHeader><TableBody>{visibleEvidence.map(item => <TableRow key={item.qa_id} data-state={selectedEvidence.has(item.qa_id) ? 'selected' : undefined}><TableCell className="align-top pt-4"><Checkbox checked={selectedEvidence.has(item.qa_id)} disabled={Boolean(busy)} onCheckedChange={checked => selectEvidence(item.qa_id, Boolean(checked))} aria-label={`选择原句 ${item.qa_id}`} /></TableCell><TableCell className="whitespace-normal py-3 align-top"><button className="block w-full text-left text-sm leading-6 [overflow-wrap:anywhere] hover:underline" onClick={() => onOpenPair(normalizePair(item))}>{item.question}</button>{item.reason && <p className="mt-2 text-xs leading-5 text-muted-foreground">归并依据：{item.reason}</p>}<p className="mt-1.5 text-xs text-muted-foreground">问答 {item.qa_id} · {customerLabel(item.customer_name, item.customer_id)}</p></TableCell><TableCell className="whitespace-normal py-3 align-top text-xs leading-5 text-muted-foreground"><p>{item.group_name || '未分组'}</p><p>{item.staff_name || '未记录客服'}</p><p className="mt-1.5">{item.asked_at_text || '时间未知'}</p></TableCell><TableCell className="py-3 align-top"><Badge variant={item.status === 'pending' ? 'secondary' : 'outline'} className="text-xs font-normal">{item.status === 'pending' ? '待核对' : '已确认'}</Badge></TableCell><TableCell className="py-2 align-top"><Button type="button" size="xs" variant="outline" aria-label={`跳转到原句 ${item.qa_id} 的会话`} onClick={() => onOpenPair(normalizePair(item))}>跳转</Button></TableCell></TableRow>)}</TableBody></Table></div> : <div className="rounded-lg border border-dashed px-5 py-9 text-center text-sm text-muted-foreground">{evidenceStatus === 'all' ? '当前范围尚无关联原句，可在新增会话整理后继续归入。' : '当前范围没有这种审核状态的原句。'}</div>}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground"><span>{detail.loading ? '正在读取原句…' : detail.error ? '原句读取失败' : `第 ${evidenceTotal ? currentEvidencePage * 8 + 1 : 0}—${Math.min((currentEvidencePage + 1) * 8, evidenceTotal)} 条 / ${evidenceTotal} 条`} · 翻页后清除当前页勾选</span><div className="flex items-center gap-2"><span>{detail.loading || detail.error ? '— / —' : `${currentEvidencePage + 1}/${evidencePages}`}</span><Button size="icon-xs" variant="outline" disabled={detail.loading || Boolean(detail.error) || currentEvidencePage === 0} onClick={() => setEvidencePage(currentEvidencePage - 1)} aria-label="上一页原句"><ChevronLeft /></Button><Button size="icon-xs" variant="outline" disabled={detail.loading || Boolean(detail.error) || currentEvidencePage >= evidencePages - 1} onClick={() => setEvidencePage(currentEvidencePage + 1)} aria-label="下一页原句"><ChevronRight /></Button></div></div>
    </>}</div></SheetContent></Sheet>

    <Dialog open={runOpen} onOpenChange={setRunOpen}><DialogContent><DialogHeader><DialogTitle>AI 整理当前范围</DialogTitle><DialogDescription>从会话文字提出标准问题与主题归并建议，结果进入待复核。已处理问答会自动跳过。</DialogDescription></DialogHeader><div className="rounded-lg border bg-muted/30 p-4 text-sm leading-7"><p>范围：{scopeLabel}</p><p>客户群：{filters.group || '全部客户群'} · 客服：{filters.staff || '全部客服'}</p>{filters.q && <p className="[overflow-wrap:anywhere]">原文关键词：{filters.q}</p>}<p className="mt-2 text-xs text-muted-foreground">待整理 {number(summaryData?.pending_pairs)} 条 · 模型 {summaryData?.model || '未配置'}</p></div><div className="space-y-2"><Label htmlFor="voc-run-limit">本次最多整理条数</Label><Input id="voc-run-limit" type="number" min={1} max={500} step={1} value={runLimit} onChange={event => setRunLimit(event.target.value)} disabled={Boolean(busy)} /><p className="text-xs text-muted-foreground">默认 500 条。提交后可继续浏览，任务会在后台运行。</p></div><DialogFooter><Button variant="outline" onClick={() => setRunOpen(false)} disabled={Boolean(busy)}>取消</Button><Button onClick={run} disabled={Boolean(busy) || !summaryData?.configured || running}>{busy === 'voc/run' ? <LoaderCircle className="animate-spin" /> : <Sparkles />}开始整理</Button></DialogFooter></DialogContent></Dialog>

    <Dialog open={createOpen} onOpenChange={setCreateOpen}><DialogContent className="sm:max-w-xl"><DialogHeader><DialogTitle>手动新增标准问题</DialogTitle><DialogDescription>手工整理的标准问题直接列为已确认；关联原句前不计问答次数。</DialogDescription></DialogHeader><DraftFields draft={createDraft} onChange={setCreateDraft} disabled={Boolean(busy)} prefix="voc-create" /><DialogFooter><Button variant="outline" onClick={() => setCreateOpen(false)} disabled={Boolean(busy)}>取消</Button><Button disabled={Boolean(busy)} onClick={() => { if (!requireQuestion(createDraft)) return; void mutate<{ topic: VocTopic }>('voc/topic', { ...draftBody(createDraft), status: 'confirmed' }, '标准问题已新增。', result => { acceptTopic(result.topic); setCreateOpen(false); setTab('confirmed'); setSelectedId(result.topic.id) }) }}>{busy === 'voc/topic' ? <LoaderCircle className="animate-spin" /> : <Plus />}新增并确认</Button></DialogFooter></DialogContent></Dialog>

    <Dialog open={splitOpen} onOpenChange={setSplitOpen}><DialogContent><DialogHeader><DialogTitle>将所选原句拆成新问题</DialogTitle><DialogDescription>已选 {selectedEvidence.size} 条原句。它们会移到新标准问题，并重新核算涉及问答数。</DialogDescription></DialogHeader><div className="space-y-4"><div className="space-y-2"><Label htmlFor="voc-split-question">新标准问题</Label><Input id="voc-split-question" value={splitDraft.question} maxLength={120} disabled={Boolean(busy)} onChange={event => setSplitDraft(value => ({ ...value, question: event.target.value }))} placeholder="填写这组原句共同表达的独立诉求" /></div><div className="space-y-2"><Label htmlFor="voc-split-theme">业务主题</Label><Input id="voc-split-theme" value={splitDraft.theme} maxLength={60} disabled={Boolean(busy)} onChange={event => setSplitDraft(value => ({ ...value, theme: event.target.value }))} /></div></div><DialogFooter><Button variant="outline" disabled={Boolean(busy)} onClick={() => setSplitOpen(false)}>取消</Button><Button disabled={Boolean(busy) || !selectedEvidence.size} onClick={() => updateEvidence('split')}>{busy === 'voc/evidence' ? <LoaderCircle className="animate-spin" /> : <Scissors />}确认拆分</Button></DialogFooter></DialogContent></Dialog>

    <Dialog open={mergeOpen} onOpenChange={setMergeOpen}><DialogContent><DialogHeader><DialogTitle>合并到现有标准问题</DialogTitle><DialogDescription>将本问题的全部原句合并到目标问题，包含当前筛选范围外的原句。数量会按问答 ID 去重，合并后以目标问题为准。</DialogDescription></DialogHeader><div className="rounded-lg border bg-muted/30 p-3 text-sm [overflow-wrap:anywhere]">来源：{topic?.question}</div><Input value={mergeSearch} disabled={Boolean(busy)} onChange={event => { setMergeSearch(event.target.value); setMergeTarget('') }} placeholder="搜索目标标准问题或主题" aria-label="搜索合并目标" />{mergeTopics.loading ? <LoadingState label="正在读取可合并问题…" /> : mergeTopics.error ? <ErrorState error={mergeTopics.error} retry={mergeTopics.retry} /> : <Select value={mergeTarget} onValueChange={setMergeTarget} disabled={Boolean(busy) || !candidates.length}><SelectTrigger aria-label="选择合并目标"><SelectValue placeholder={candidates.length ? '选择目标标准问题' : '没有匹配的待复核或已确认问题'} /></SelectTrigger><SelectContent>{candidates.map(item => <SelectItem key={item.id} value={String(item.id)}>#{item.id} · {item.question}</SelectItem>)}</SelectContent></Select>}{target && <p className="text-xs text-muted-foreground">目标状态：{topicLabels[target.status]} · 版本 {target.revision}</p>}<DialogFooter><Button variant="outline" disabled={Boolean(busy)} onClick={() => setMergeOpen(false)}>取消</Button><Button disabled={Boolean(busy) || !topic || !target || dirty} onClick={() => { if (!topic || !target) return; void mutate<{ topic: VocTopic }>('voc/merge', { source_id: topic.id, target_id: target.id, source_revision: draftRevision ?? topic.revision, target_revision: target.revision }, '标准问题已合并，原句数量已重新去重。', result => { acceptTopic(result.topic); setMergeOpen(false); setSelectedId(result.topic.id) }) }}>{busy === 'voc/merge' ? <LoaderCircle className="animate-spin" /> : <GitMerge />}确认合并</Button></DialogFooter></DialogContent></Dialog>

    <Dialog open={ignoreOpen} onOpenChange={setIgnoreOpen}><DialogContent><DialogHeader><DialogTitle>忽略这个标准问题？</DialogTitle><DialogDescription>该问题会移到“已忽略”，原始问答和证据不会删除。之后仍可恢复为待复核。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={Boolean(busy)} onClick={() => setIgnoreOpen(false)}>取消</Button><Button disabled={Boolean(busy)} onClick={() => saveTopic('ignored')}>{busy === 'voc/topic' && <LoaderCircle className="animate-spin" />}移到已忽略</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={excludeOpen} onOpenChange={setExcludeOpen}><DialogContent><DialogHeader><DialogTitle>从这个问题排除所选原句？</DialogTitle><DialogDescription>将排除 {selectedEvidence.size} 条不属于该标准问题的原句，并重算次数。原始会话和问答保留。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={Boolean(busy)} onClick={() => setExcludeOpen(false)}>取消</Button><Button disabled={Boolean(busy) || !selectedEvidence.size} onClick={() => updateEvidence('exclude')}>{busy === 'voc/evidence' && <LoaderCircle className="animate-spin" />}确认排除</Button></DialogFooter></DialogContent></Dialog>

    <Dialog open={refreshTopicOpen} onOpenChange={setRefreshTopicOpen}><DialogContent><DialogHeader><DialogTitle>读取最新版本？</DialogTitle><DialogDescription>尚未保存的标准问题、主题和范围说明将替换为服务端最新内容。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={() => setRefreshTopicOpen(false)}>保留编辑</Button><Button onClick={readLatestTopic}>放弃编辑并刷新</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={jobsOpen} onOpenChange={setJobsOpen}><DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-4xl"><DialogHeader><DialogTitle>AI 整理记录</DialogTitle><DialogDescription>当前数据源的任务记录；统计按各任务提交时范围计算。</DialogDescription></DialogHeader>{history.loading ? <LoadingState label="正在读取整理记录…" /> : history.error ? <ErrorState error={history.error} retry={history.retry} /> : !history.data?.items.length ? <p className="py-10 text-center text-sm text-muted-foreground">暂无 AI 整理记录。</p> : <div className="overflow-hidden rounded-lg border"><Table className="table-fixed min-w-[760px]"><TableHeader><TableRow><TableHead className="w-14">任务</TableHead><TableHead className="w-36">创建时间</TableHead><TableHead className="w-28">状态</TableHead><TableHead className="w-24 text-right">成功/总量</TableHead><TableHead className="w-20 text-right">失败</TableHead><TableHead className="w-20 text-right">跳过</TableHead><TableHead>模型 / 说明</TableHead></TableRow></TableHeader><TableBody>{history.data.items.map(job => <TableRow key={job.id}><TableCell className="align-top tabular-nums">#{job.id}</TableCell><TableCell className="whitespace-normal align-top text-xs text-muted-foreground">{job.created_at || '时间未知'}</TableCell><TableCell className="align-top"><Badge variant="outline" className="font-normal">{jobLabels[job.status]}</Badge></TableCell><TableCell className="text-right align-top tabular-nums">{number(job.processed)}/{number(job.total)}</TableCell><TableCell className="text-right align-top tabular-nums">{number(job.failed)}</TableCell><TableCell className="text-right align-top tabular-nums">{number(job.skipped)}</TableCell><TableCell className="whitespace-normal align-top text-xs leading-5 text-muted-foreground [overflow-wrap:anywhere]"><p>{job.model}</p>{job.error && <p className="mt-1.5 text-destructive">{job.error}</p>}<p className="mt-1.5">{number(job.usage_tokens)} tokens</p></TableCell></TableRow>)}</TableBody></Table></div>}<DialogFooter><Button variant="outline" onClick={history.retry} disabled={history.loading}><RefreshCw />刷新记录</Button><Button onClick={() => setJobsOpen(false)}>完成</Button></DialogFooter></DialogContent></Dialog>
  </div>
}

function ClipboardIcon() { return <FileText className="size-4 text-muted-foreground" /> }

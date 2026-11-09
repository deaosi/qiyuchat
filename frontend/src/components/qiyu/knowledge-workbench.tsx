import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowUpRight, BookOpen, BrainCircuit, CheckCheck, ChevronLeft, ChevronRight,
  CircleCheck, Clock3, Download, EyeOff, FileText, History, LoaderCircle,
  MessageSquareText, RefreshCw, Search, ShieldCheck, Sparkles, TriangleAlert, X,
} from 'lucide-react'
import { toast } from 'sonner'
import { useApi } from '@/hooks/use-api'
import {
  ApiError, apiPost, downloadKnowledgeExport, normalizePair, type ApiFilters,
  type Knowledge, type KnowledgeDetail, type KnowledgeJob, type KnowledgeStatus,
  type VocSummary, type VocTopic, type VocTopicList,
} from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
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

export interface KnowledgeRequest { topic: VocTopic; token: number }
interface Props { filters: ApiFilters; refreshKey?: number; onOpenPair: (pair: QAPair) => void; request?: KnowledgeRequest | null; onRequestConsumed?: () => void; onQuestions: () => void }
interface Draft { question: string; answer: string; scope: string; cautionText: string }
const emptyDraft: Draft = { question: '', answer: '', scope: '', cautionText: '' }
const labels: Record<KnowledgeStatus, string> = { draft: '待审核', confirmed: '已确认', ignored: '已忽略' }
const jobLabels: Record<KnowledgeJob['status'], string> = { queued: '排队中', running: '正在整理', completed: '已完成', partial: '部分完成', failed: '整理失败' }
const fmt = (n: number) => n.toLocaleString('zh-CN')
const active = (job: KnowledgeJob | null | undefined) => job?.status === 'queued' || job?.status === 'running'
const textareaClass = 'flex w-full rounded-md border bg-background px-3 py-2 text-sm leading-7 shadow-xs outline-none transition-[color,box-shadow] placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50'
const toDraft = (value: Knowledge): Draft => ({ question: value.question, answer: value.answer, scope: value.scope, cautionText: value.cautions.join('\n') })

function Status({ status }: { status: KnowledgeStatus }) { const Icon = status === 'confirmed' ? CircleCheck : status === 'ignored' ? EyeOff : Clock3; return <Badge variant="outline" className="gap-1.5 font-normal"><Icon className="size-3.5" />{labels[status]}</Badge> }
function Loading() { return <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground" role="status"><LoaderCircle className="size-4 animate-spin" />正在读取知识资产…</div> }
function Failure({ error, retry }: { error: string | null; retry: () => void }) { return <div className="flex flex-col items-center gap-3 px-5 py-10 text-center" role="alert"><TriangleAlert className="size-5 text-muted-foreground" /><p className="max-w-lg whitespace-pre-wrap text-sm text-muted-foreground [overflow-wrap:anywhere]">{error || '读取失败，请重试。'}</p><Button variant="outline" size="sm" onClick={retry}><RefreshCw />重试</Button></div> }

export function KnowledgeWorkbench({ filters, refreshKey = 0, onOpenPair, request, onRequestConsumed, onQuestions }: Props) {
  const [tab, setTab] = useState<KnowledgeStatus>('draft')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [refresh, setRefresh] = useState(0)
  const [poll, setPoll] = useState(0)
  const [jobsSnapshot, setJobsSnapshot] = useState<KnowledgeJob[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detailRefresh, setDetailRefresh] = useState(0)
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const [busy, setBusy] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [runOpen, setRunOpen] = useState(false)
  const [runTopic, setRunTopic] = useState('')
  const [requestedTopic, setRequestedTopic] = useState<VocTopic | null>(null)
  const [runLimit, setRunLimit] = useState('100')
  const [jobHistoryOpen, setJobHistoryOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)
  const [ignoreOpen, setIgnoreOpen] = useState(false)
  const [discardOpen, setDiscardOpen] = useState(false)
  const [evidencePage, setEvidencePage] = useState(0)
  const [allQuotes, setAllQuotes] = useState(false)
  const [submittedJob, setSubmittedJob] = useState<KnowledgeJob | null>(null)
  const mounted = useRef(true)
  const seenActive = useRef<number | null>(null)
  const source = filters.source
  const assetFilters = useMemo<ApiFilters>(() => ({ source }), [source])
  const revision = refreshKey + refresh
  const summary = useApi<VocSummary>('voc/summary', filters, revision)
  const list = useApi<{ items: Knowledge[]; total: number }>('voc/knowledge', { source, status: tab }, revision)
  const detail = useApi<KnowledgeDetail>('voc/knowledge/detail', { source, id: selectedId ?? 0 }, detailRefresh, selectedId !== null)
  const jobs = useApi<{ items: KnowledgeJob[] }>('voc/knowledge/jobs', assetFilters, revision + poll)
  const topics = useApi<VocTopicList>('voc/topics', { ...filters, status: 'all' }, revision, runOpen)
  const jobItems = jobs.data?.items || jobsSnapshot
  const latestKnown = jobItems.slice().sort((a, b) => b.id - a.id)[0] || null
  const latestJob = latestKnown && (!submittedJob || latestKnown.id >= submittedJob.id) ? latestKnown : submittedJob || latestKnown
  const running = active(latestJob)
  const knowledge = detail.data?.knowledge
  const evidence = detail.data?.evidence || []
  const hasSourceAnswer = evidence.some(item => typeof item.answer === 'string' && item.answer.trim())
  const riskCount = (knowledge?.conflicts.length || 0) + (knowledge?.missing_info.length || 0)
  const dirty = Boolean(knowledge && (draft.question !== knowledge.question || draft.answer !== knowledge.answer || draft.scope !== knowledge.scope || draft.cautionText !== knowledge.cautions.join('\n')))
  const filtered = (list.data?.items || []).filter(item => `${item.question} ${item.answer} ${item.scope}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  const pages = Math.max(1, Math.ceil(filtered.length / 8)), currentPage = Math.min(page, pages - 1)
  const rows = filtered.slice(currentPage * 8, (currentPage + 1) * 8)
  const evidencePages = Math.max(1, Math.ceil(evidence.length / 5)), currentEvidencePage = Math.min(evidencePage, evidencePages - 1)
  const scopeLabel = filters.start || filters.end ? `${filters.start || '不限起始'} 至 ${filters.end || '不限结束'}` : filters.days ? `近 ${filters.days} 天` : '全部时间'
  const noCurrentReplies = summary.data?.answered_pairs === 0
  const topicOptions = (topics.data?.items || []).filter(item => item.status !== 'ignored' && item.count > 0 && item.answered_count !== 0)
  const selectedTopic = topicOptions.find(item => String(item.id) === runTopic) || (!topics.data && requestedTopic?.answered_count !== 0 && String(requestedTopic?.id) === runTopic ? requestedTopic : null)

  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => { setPage(0) }, [tab, search, source])
  useEffect(() => { if (jobs.data) setJobsSnapshot(jobs.data.items) }, [jobs.data])
  useEffect(() => {
    if (!request) return
    setRequestedTopic(request.topic); setRunTopic(String(request.topic.id)); setRunLimit('100'); setRunOpen(true)
    onRequestConsumed?.()
  }, [request?.token])
  useEffect(() => { if (detail.data) { setDraft(toDraft(detail.data.knowledge)); setEvidencePage(0); setAllQuotes(false) } }, [detail.data])
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setPoll(value => value + 1), 2500)
    return () => window.clearInterval(timer)
  }, [running, source])
  useEffect(() => {
    if (!latestJob) return
    if (active(latestJob)) { seenActive.current = latestJob.id; return }
    if (seenActive.current !== latestJob.id) return
    seenActive.current = null; setRefresh(value => value + 1)
    if (latestJob.status === 'completed') {
      toast.success('知识草稿已整理完成，请核对答复、适用条件和来源。')
    } else toast.error(latestJob.error || '知识整理未全部完成，请查看记录后重试。')
  }, [latestJob])

  function validate(status: KnowledgeStatus) {
    const q = draft.question.trim(), a = draft.answer.trim(), s = draft.scope.trim()
    const cautions = draft.cautionText.split('\n').map(value => value.trim()).filter(Boolean)
    if ([...q].length < 3 || [...q].length > 120) { toast.error('标准问题须为 3–120 字。'); return null }
    if ([...a].length > 4000) { toast.error('综合答复不能超过 4000 字。'); return null }
    if ([...s].length > 1000) { toast.error('适用条件不能超过 1000 字。'); return null }
    if (cautions.length > 20 || cautions.some(value => [...value].length > 500)) { toast.error('注意事项最多 20 条，每条不超过 500 字。'); return null }
    if (status === 'confirmed' && !a) { toast.error('综合答复为空，不能确认知识。'); return null }
    if (status === 'confirmed' && !hasSourceAnswer) { toast.error('来源中没有有效客服回复，本条只能保留为草稿。'); return null }
    return { question: q, answer: a, scope: s, cautions }
  }
  async function save(status: KnowledgeStatus) {
    if (!knowledge || busy) return
    const values = validate(status); if (!values) return
    setBusy('save')
    try {
      await apiPost<{ knowledge: Knowledge }>('voc/knowledge/save', assetFilters, JSON.stringify({ id: knowledge.id, ...values, status, revision: knowledge.revision }), { 'Content-Type': 'application/json' })
      if (!mounted.current) return
      setConfirmOpen(false); setIgnoreOpen(false); setRefresh(value => value + 1); setDetailRefresh(value => value + 1); setTab(status)
      toast.success(status === 'confirmed' ? '知识已人工确认，未自动发布到外部知识库。' : status === 'ignored' ? '知识草稿已移到已忽略。' : '知识已保存为待审核草稿。')
    } catch (error) {
      if (!mounted.current || error instanceof ApiError && error.status === 401) return
      if (error instanceof ApiError && error.status === 409) { setDetailRefresh(value => value + 1); setRefresh(value => value + 1); setConfirmOpen(false); setIgnoreOpen(false); toast.warning('知识已有更新，已刷新最新版本。请核对后重新保存。') }
      else toast.error(error instanceof Error ? error.message : '保存失败，请重试。')
    } finally { if (mounted.current) setBusy(null) }
  }
  function confirm() {
    if (!validate('confirmed')) return
    if (riskCount) { setAcknowledged(false); setConfirmOpen(true) } else void save('confirmed')
  }
  async function run() {
    const limit = Number(runLimit)
    if (noCurrentReplies || selectedTopic?.answered_count === 0) { toast.error('当前范围没有可用客服回复，请补充来源后再整理知识。'); return }
    if (topics.loading || !topics.data || topics.error) { toast.error('请等来源问题读取完成后再生成。'); return }
    if (!runTopic || !selectedTopic) { toast.error('请选择有原句证据的来源问题。'); return }
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) { toast.error('单次来源问答须为 1–100 条。'); return }
    setBusy('run')
    try {
      const result = await apiPost<{ job: KnowledgeJob }>('voc/knowledge/run', filters, JSON.stringify({ topic_id: Number(runTopic), limit }), { 'Content-Type': 'application/json' })
      if (!mounted.current) return
      setSubmittedJob(result.job); setRunOpen(false); setRefresh(value => value + 1)
      toast.success('知识整理任务已提交，结果将保存为待审核草稿。')
    } catch (error) { if (mounted.current && !(error instanceof ApiError && error.status === 401)) toast.error(error instanceof Error ? error.message : '无法提交知识整理。') }
    finally { if (mounted.current) setBusy(null) }
  }
  async function exportCsv() {
    setExporting(true)
    try { await downloadKnowledgeExport(source); toast.success('已下载当前数据源的已确认知识。') }
    catch (error) { if (!(error instanceof ApiError && error.status === 401)) toast.error(error instanceof Error ? error.message : '导出失败。') }
    finally { if (mounted.current) setExporting(false) }
  }
  function openItem(item: Knowledge) { setSelectedId(item.id); setEvidencePage(0); setAllQuotes(false) }
  const done = latestJob ? latestJob.processed + latestJob.failed + latestJob.skipped : 0

  return <div className="min-w-0 space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-4"><div><h2 className="text-lg font-semibold">把客户问题与日常回复沉淀为知识</h2><p className="mt-1 text-sm text-muted-foreground">整理综合答复、适用条件和回复范式，保留冲突与生成依据，人工确认后再使用。</p></div><div className="flex flex-wrap gap-2"><Button variant="outline" size="sm" onClick={() => setJobHistoryOpen(true)}><History />整理记录</Button><Button variant="outline" size="sm" disabled={exporting} onClick={() => void exportCsv()}>{exporting ? <LoaderCircle className="animate-spin" /> : <Download />}导出已确认知识</Button><Button size="sm" disabled={Boolean(busy) || running || !summary.data?.configured || noCurrentReplies} onClick={() => { setRunTopic(''); setRequestedTopic(null); setRunLimit('100'); setRunOpen(true) }}>{running ? <LoaderCircle className="animate-spin" /> : <BrainCircuit />}{running ? '正在整理知识' : '选择问题整理知识'}</Button></div></div>
    <div className="flex items-start gap-2 rounded-lg border bg-muted/35 px-4 py-3 text-sm leading-6 text-muted-foreground"><BookOpen className="mt-1 size-4 shrink-0" /><p>这里是当前数据源已沉淀的知识资产，不随上方日期、客户群和客服筛选隐藏。当前筛选只用于选取新知识的生成依据；确认不会自动发布到店铺知识库。</p></div>
    {noCurrentReplies && <div className="flex items-start gap-2 rounded-lg border bg-muted/40 px-4 py-3 text-sm leading-6" role="status"><TriangleAlert className="mt-1 size-4 shrink-0 text-muted-foreground" /><p>当前会话范围没有可用客服回复。可先整理客户高频问题，补充客服侧会话或导入含回答的记录后，再沉淀知识。<span className="mt-1 block text-xs text-muted-foreground">已沉淀知识仍可查看、编辑和导出。</span></p></div>}
    {latestJob && <Card className="gap-3 py-4"><CardHeader><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2"><CardTitle className="text-sm">最近知识整理</CardTitle><Badge variant="outline" className="font-normal">{running && <LoaderCircle className="mr-1 size-3 animate-spin" />}{jobLabels[latestJob.status]}</Badge></div><span className="text-xs text-muted-foreground">任务 #{latestJob.id} · 来源问题 #{latestJob.topic_id}</span></div></CardHeader><CardContent className="space-y-2"><Progress value={latestJob.total ? Math.min(100, done / latestJob.total * 100) : 0} className="h-1.5" aria-label={`已处理 ${done}/${latestJob.total} 条来源问答`} /><div className="flex flex-wrap justify-between gap-2 text-xs text-muted-foreground"><span>成功覆盖 {fmt(latestJob.processed)} / {fmt(latestJob.total)} · 失败 {fmt(latestJob.failed)} · 跳过 {fmt(latestJob.skipped)}</span>{latestJob.knowledge_id && <button className="font-medium text-foreground hover:underline" onClick={() => setSelectedId(latestJob.knowledge_id!)}>审核生成草稿 →</button>}</div>{latestJob.error && <p className="whitespace-pre-wrap text-sm text-destructive [overflow-wrap:anywhere]" role="alert">{latestJob.error}</p>}</CardContent></Card>}
    {jobs.error && <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border px-4 py-3 text-sm text-muted-foreground" role="alert"><span>暂时无法更新整理任务状态{latestJob ? '，以上显示上次读取的状态' : ''}：{jobs.error}</span><Button variant="outline" size="sm" onClick={jobs.retry} disabled={jobs.loading}><RefreshCw />重试</Button></div>}
    <Card className="min-w-0 gap-4"><CardHeader><div className="flex flex-wrap items-center justify-between gap-3"><Tabs value={tab} onValueChange={value => setTab(value as KnowledgeStatus)}><TabsList><TabsTrigger value="draft">待审核</TabsTrigger><TabsTrigger value="confirmed">已确认</TabsTrigger><TabsTrigger value="ignored">已忽略</TabsTrigger></TabsList></Tabs><Button size="sm" variant="ghost" disabled={list.loading} onClick={() => setRefresh(value => value + 1)}><RefreshCw className={list.loading ? 'animate-spin' : ''} />刷新</Button></div><div className="relative mt-2 max-w-md"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input value={search} onChange={event => setSearch(event.target.value)} maxLength={200} placeholder="搜索知识问题、综合答复或适用条件…" aria-label="搜索知识资产" className="pl-9 pr-9" />{search && <Button size="icon-xs" variant="ghost" className="absolute right-1.5 top-1/2 -translate-y-1/2" onClick={() => setSearch('')} aria-label="清空知识搜索"><X /></Button>}</div></CardHeader><CardContent className="min-w-0">{list.loading ? <Loading /> : list.error ? <Failure error={list.error} retry={list.retry} /> : !filtered.length ? <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed px-5 py-12 text-center"><BookOpen className="size-7 text-muted-foreground/60" /><h3 className="text-sm font-medium">{search ? '没有匹配的知识' : `暂无${labels[tab]}知识`}</h3><p className="max-w-lg text-sm leading-6 text-muted-foreground">从高频问题中选择一项，点击“整理成知识”，将客户诉求和客服日常回复整理为可审核草稿。</p><Button variant="outline" size="sm" onClick={onQuestions}>选择高频问题<ArrowUpRight /></Button></div> : <div className="overflow-hidden rounded-lg border"><Table className="table-fixed min-w-[820px]"><TableHeader><TableRow><TableHead className="w-[30%]">标准问题</TableHead><TableHead>综合答复摘要</TableHead><TableHead className="w-24 text-right">来源问答</TableHead><TableHead className="w-32">核对项</TableHead><TableHead className="w-36">更新时间</TableHead><TableHead className="w-10" /></TableRow></TableHeader><TableBody>{rows.map(item => { const risks = item.conflicts.length + item.missing_info.length; return <TableRow key={item.id}><TableCell className="min-w-0 whitespace-normal py-4 align-top"><button className="block w-full text-left font-medium leading-6 [overflow-wrap:anywhere] hover:underline" onClick={() => openItem(item)}>{item.question}</button><p className="mt-1.5 text-xs text-muted-foreground">知识 #{item.id} · 来源标准问题 #{item.topic_id}</p></TableCell><TableCell className="whitespace-normal py-4 align-top"><p className="line-clamp-3 text-sm leading-6 text-muted-foreground [overflow-wrap:anywhere]">{item.answer || '未形成可用答复，需补充来源后继续整理。'}</p></TableCell><TableCell className="py-4 text-right align-top tabular-nums">{item.evidence_ids.length}</TableCell><TableCell className="py-4 align-top">{risks ? <Badge variant="secondary" className="font-normal">{item.status === 'confirmed' ? '核对记录' : '待核对'} {risks}</Badge> : <span className="text-xs text-muted-foreground">无标记冲突</span>}{!item.answer.trim() && <p className="mt-2 text-xs text-destructive">答复为空</p>}</TableCell><TableCell className="whitespace-normal py-4 align-top text-xs text-muted-foreground">{item.updated_at || '时间未知'}</TableCell><TableCell className="py-3 align-top"><Button size="icon-xs" variant="ghost" onClick={() => openItem(item)} aria-label={`审核知识 ${item.id}`}><ArrowUpRight /></Button></TableCell></TableRow> })}</TableBody></Table></div>}</CardContent><CardFooter className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground"><span>当前{labels[tab]}列表匹配 {fmt(filtered.length)} 条 · 每次重新整理生成独立草稿</span><div className="flex items-center gap-2"><span>{currentPage + 1}/{pages}</span><Button size="icon-xs" variant="outline" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)} aria-label="上一页知识"><ChevronLeft /></Button><Button size="icon-xs" variant="outline" disabled={currentPage >= pages - 1} onClick={() => setPage(currentPage + 1)} aria-label="下一页知识"><ChevronRight /></Button></div></CardFooter></Card>

    <Sheet open={selectedId !== null} onOpenChange={open => { if (!open) { if (dirty) setDiscardOpen(true); else setSelectedId(null) } }}><SheetContent className="w-full gap-0 sm:max-w-[900px]"><SheetHeader className="shrink-0 border-b p-6 pr-12"><SheetTitle className="flex flex-wrap items-center gap-2 text-lg">知识审核{knowledge && <Status status={knowledge.status} />}</SheetTitle><SheetDescription>核对综合答复、适用条件和客服回复依据，确认仅沉淀为知识资产。</SheetDescription></SheetHeader><div className="min-h-0 min-w-0 flex-1 overflow-y-auto px-6 pb-7 [overflow-wrap:anywhere]">{detail.loading ? <Loading /> : detail.error ? <Failure error={detail.error} retry={detail.retry} /> : knowledge && <>
      <div className="flex flex-wrap items-center justify-between gap-3 py-4 text-xs text-muted-foreground"><span>知识 #{knowledge.id} · 版本 {knowledge.revision} · 来源问题 #{knowledge.topic_id}</span><span>整理模型 · {knowledge.model}</span></div>
      {(riskCount > 0 || !draft.answer.trim() || !hasSourceAnswer) && <div className="mb-5 space-y-3 rounded-lg border bg-muted/40 p-4"><h3 className="flex items-center gap-2 text-sm font-semibold"><TriangleAlert className="size-4" />{knowledge.status === 'confirmed' ? '来源核对记录' : '这些内容仍需核对'}</h3>{!draft.answer.trim() && <p className="text-sm text-destructive">综合答复为空，不能确认知识。</p>}{!hasSourceAnswer && <p className="text-sm text-destructive">生成依据中没有有效客服回复，目前只能保留草稿。请补充来源后重新整理。</p>}{knowledge.conflicts.map((item, index) => <div key={`conflict-${index}`} className="text-sm leading-6"><p><span className="font-medium">冲突说法：</span>{item.description}</p><p className="mt-1 text-xs text-muted-foreground">来源问答 {item.qa_ids.join('、') || '未标明'}</p></div>)}{knowledge.missing_info.length > 0 && <div className="text-sm leading-6"><p className="font-medium">缺失信息</p><ul className="mt-1 list-disc space-y-1 pl-5">{knowledge.missing_info.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}<p className="text-xs leading-5 text-muted-foreground">请在答复、适用条件或注意事项中处理上述差异，原始冲突记录会继续保留供追溯。</p></div>}
      <div className="space-y-5"><div className="space-y-2"><Label htmlFor="knowledge-question">标准问题（3–120 字）</Label><Input id="knowledge-question" value={draft.question} maxLength={120} disabled={Boolean(busy)} onChange={event => setDraft(value => ({ ...value, question: event.target.value }))} /></div><div className="space-y-2"><Label htmlFor="knowledge-answer">综合答复<span className="text-muted-foreground">（最多 4000 字）</span></Label><textarea id="knowledge-answer" rows={7} maxLength={4000} value={draft.answer} disabled={Boolean(busy)} onChange={event => setDraft(value => ({ ...value, answer: event.target.value }))} className={textareaClass} placeholder="结合客服实际回复，形成可直接使用且边界清楚的答复" /></div><div className="space-y-2"><Label htmlFor="knowledge-scope">适用条件<span className="text-muted-foreground">（最多 1000 字）</span></Label><textarea id="knowledge-scope" rows={3} maxLength={1000} value={draft.scope} disabled={Boolean(busy)} onChange={event => setDraft(value => ({ ...value, scope: event.target.value }))} className={textareaClass} placeholder="适用商品、订单状态、时间范围或其他必要前提" /></div><div className="space-y-2"><Label htmlFor="knowledge-cautions">注意事项<span className="text-muted-foreground">（每行一条，最多 20 条，每条不超过 500 字）</span></Label><textarea id="knowledge-cautions" rows={3} maxLength={10020} value={draft.cautionText} disabled={Boolean(busy)} onChange={event => setDraft(value => ({ ...value, cautionText: event.target.value }))} className={textareaClass} placeholder="保留需要人工确认、不能承诺或存在例外的内容" /></div></div>
      <div className="mt-5 flex flex-wrap items-center gap-2"><Button size="sm" variant="outline" disabled={Boolean(busy) || !dirty} onClick={() => void save('draft')}>{busy === 'save' && <LoaderCircle className="animate-spin" />}{knowledge.status === 'confirmed' ? '保存修改并转待审核' : '保存草稿'}</Button><Button size="sm" disabled={Boolean(busy) || !draft.answer.trim() || !hasSourceAnswer} onClick={confirm}><CheckCheck />确认当前知识</Button>{knowledge.status !== 'ignored' ? <Button size="sm" variant="ghost" disabled={Boolean(busy)} onClick={() => setIgnoreOpen(true)}><EyeOff />忽略</Button> : <Button size="sm" variant="outline" disabled={Boolean(busy)} onClick={() => void save('draft')}>恢复待审核</Button>}<span className="text-xs text-muted-foreground">不自动发布到外部知识库。</span></div>
      <Separator className="my-6" /><section><h3 className="flex items-center gap-2 text-sm font-semibold"><MessageSquareText className="size-4" />客服日常回复范式</h3><p className="mt-1 text-xs text-muted-foreground">按实际答法整理，结合适用条件使用；下列来源标记保留供核对。</p>{knowledge.reply_patterns.length ? <div className="mt-4 space-y-3">{knowledge.reply_patterns.map((item, index) => <div key={index} className="rounded-lg border p-4"><h4 className="mb-2 text-sm font-medium">{item.label}</h4><p className="whitespace-pre-wrap text-sm leading-7">{item.text}</p><div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"><span>依据</span>{item.qa_ids.map(id => { const pair = evidence.find(row => Number(row.qa_id) === id); return pair ? <button key={id} className="underline underline-offset-4" onClick={() => onOpenPair(normalizePair(pair))}>问答 {id}</button> : <span key={id}>问答 {id}</span> })}</div></div>)}</div> : <p className="mt-4 text-sm text-muted-foreground">未整理出独立回复范式，请结合下方原始答复复核。</p>}</section>
      {Boolean(knowledge.source_quotes?.length) && <><Separator className="my-6" /><section><h3 className="flex items-center gap-2 text-sm font-semibold"><FileText className="size-4" />关键来源原句</h3><div className="mt-4 space-y-3">{knowledge.source_quotes!.slice(0, allQuotes ? undefined : 5).map((item, index) => <blockquote key={index} className="border-l-2 pl-4 text-sm leading-7"><p className="whitespace-pre-wrap">{item.quote}</p><footer className="mt-1 text-xs text-muted-foreground">问答 {item.qa_id} · 生成时引用</footer></blockquote>)}</div>{knowledge.source_quotes!.length > 5 && <Button variant="ghost" size="sm" className="mt-3" onClick={() => setAllQuotes(value => !value)}>{allQuotes ? '收起引用' : `展开全部 ${knowledge.source_quotes!.length} 条引用`}</Button>}</section></>}
      <Separator className="my-6" /><section><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="size-4" />生成依据 · 脱敏快照</h3><span className="text-xs text-muted-foreground">{evidence.length} 条来源问答</span></div><p className="mt-2 text-xs leading-5 text-muted-foreground">这里保留生成当时的问题与客服回复。后续会话可能更新或归档，“查看当前会话”用于核对最新原文。</p><div className="mt-4 space-y-4">{evidence.slice(currentEvidencePage * 5, (currentEvidencePage + 1) * 5).map((item, index) => <div key={`${String(item.qa_id)}-${index}`} className="rounded-lg border p-4"><div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>问答 {String(item.qa_id)} · {String(item.customer_name || '匿名客户')} · {String(item.group_name || '未分组')}</span><Button size="xs" variant="ghost" onClick={() => onOpenPair(normalizePair(item))}>查看当前会话<ArrowUpRight /></Button></div><p className="whitespace-pre-wrap text-sm leading-7"><span className="mr-2 font-medium">问</span>{String(item.question || '')}</p><p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-muted-foreground"><span className="mr-2 font-medium text-foreground">答</span>{typeof item.answer === 'string' && item.answer.trim() ? item.answer : '来源中没有有效客服回复'}</p><p className="mt-3 text-xs text-muted-foreground">{String(item.asked_at_text || '时间未知')} · {String(item.staff_name || '未记录客服')}</p></div>)}</div>{!evidence.length && <p className="py-6 text-sm text-muted-foreground">暂无可展示的来源快照，请核对原始依据后再使用。</p>}<div className="mt-4 flex items-center justify-end gap-2 text-xs text-muted-foreground"><span>{currentEvidencePage + 1}/{evidencePages}</span><Button size="icon-xs" variant="outline" disabled={currentEvidencePage === 0} onClick={() => setEvidencePage(currentEvidencePage - 1)} aria-label="上一页知识依据"><ChevronLeft /></Button><Button size="icon-xs" variant="outline" disabled={currentEvidencePage >= evidencePages - 1} onClick={() => setEvidencePage(currentEvidencePage + 1)} aria-label="下一页知识依据"><ChevronRight /></Button></div></section>
    </>}</div></SheetContent></Sheet>

    <Dialog open={runOpen} onOpenChange={setRunOpen}><DialogContent><DialogHeader><DialogTitle>整理问题与客服回复</DialogTitle><DialogDescription>根据所选标准问题的真实原句与客服日常答复，生成待审核知识草稿。不会覆盖已确认知识。</DialogDescription></DialogHeader>{topics.loading ? <Loading /> : topics.error ? <Failure error={topics.error} retry={topics.retry} /> : <div className="space-y-4"><div className="space-y-2"><Label>来源标准问题</Label><Select value={runTopic} onValueChange={setRunTopic} disabled={Boolean(busy)}><SelectTrigger className="w-full" aria-label="选择知识来源问题"><SelectValue placeholder="选择有客服回复的标准问题">{selectedTopic?.question}</SelectValue></SelectTrigger><SelectContent>{topicOptions.map(item => <SelectItem key={item.id} value={String(item.id)}>#{item.id} · {item.question}</SelectItem>)}</SelectContent></Select>{!topicOptions.length && <p className="text-xs leading-5 text-muted-foreground">当前范围没有包含可用客服回复的标准问题，请补充客服侧会话或导入含回答的记录。</p>}</div><div className="rounded-lg border bg-muted/30 p-3 text-sm leading-7"><p>生成依据范围：{scopeLabel}</p><p>客户群：{filters.group || '全部客户群'} · 客服：{filters.staff || '全部客服'}</p>{filters.q && <p>原文关键词：{filters.q}</p>}<p className="mt-1 text-xs text-muted-foreground">{selectedTopic?.status === 'pending' ? '来源标准问题尚待复核，请一并核对归并是否准确。' : '将保留生成时的脱敏依据快照。'}</p></div><div className="space-y-2"><Label htmlFor="knowledge-run-limit">最多使用来源问答数</Label><Input id="knowledge-run-limit" type="number" min={1} max={100} step={1} value={runLimit} onChange={event => setRunLimit(event.target.value)} disabled={Boolean(busy)} /><p className="text-xs text-muted-foreground">默认最多 100 条 · 整理模型 {summary.data?.model || '未配置'}</p></div></div>}<DialogFooter><Button variant="outline" disabled={Boolean(busy)} onClick={() => setRunOpen(false)}>取消</Button><Button disabled={Boolean(busy) || running || !selectedTopic || !summary.data?.configured || noCurrentReplies || topics.loading || !topics.data || Boolean(topics.error)} onClick={() => void run()}>{busy === 'run' ? <LoaderCircle className="animate-spin" /> : <Sparkles />}生成知识草稿</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}><DialogContent className="max-h-[85dvh] overflow-y-auto"><DialogHeader><DialogTitle>确认已核对来源差异</DialogTitle><DialogDescription>本条知识存在生成时标记的冲突或缺失信息。请核对具体内容，并在综合答复、适用条件或注意事项中作出处理。</DialogDescription></DialogHeader><div className="space-y-4 rounded-lg border bg-muted/30 p-4 text-sm leading-6">{knowledge?.conflicts.map((item, index) => <div key={index}><p className="font-medium">冲突 {index + 1}</p><p>{item.description}</p><p className="mt-1 text-xs text-muted-foreground">来源问答 {item.qa_ids.join('、') || '未标明'}</p></div>)}{knowledge?.missing_info.length ? <div><p className="font-medium">缺失信息</p><ul className="mt-1 list-disc space-y-1 pl-5">{knowledge.missing_info.map((item, index) => <li key={index}>{item}</li>)}</ul></div> : null}</div><label className="flex items-start gap-3 text-sm leading-6"><Checkbox checked={acknowledged} onCheckedChange={value => setAcknowledged(Boolean(value))} className="mt-1" /><span>我已核对上述来源差异，并在知识内容中明确适用边界或需要继续确认的事项。</span></label><DialogFooter><Button variant="outline" disabled={Boolean(busy)} onClick={() => setConfirmOpen(false)}>继续编辑</Button><Button disabled={Boolean(busy) || !acknowledged || !draft.answer.trim() || !hasSourceAnswer} onClick={() => void save('confirmed')}>{busy === 'save' && <LoaderCircle className="animate-spin" />}确认当前知识</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={ignoreOpen} onOpenChange={setIgnoreOpen}><DialogContent><DialogHeader><DialogTitle>忽略这条知识草稿？</DialogTitle><DialogDescription>知识和生成依据会继续保留，可在“已忽略”中恢复审核。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={Boolean(busy)} onClick={() => setIgnoreOpen(false)}>取消</Button><Button disabled={Boolean(busy)} onClick={() => void save('ignored')}>移到已忽略</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={discardOpen} onOpenChange={setDiscardOpen}><DialogContent><DialogHeader><DialogTitle>放弃尚未保存的修改？</DialogTitle><DialogDescription>本次文字编辑尚未保存，关闭后会保留服务端原有版本。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={() => setDiscardOpen(false)}>继续编辑</Button><Button onClick={() => { setDiscardOpen(false); setSelectedId(null) }}>放弃修改并关闭</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={jobHistoryOpen} onOpenChange={setJobHistoryOpen}><DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-3xl"><DialogHeader><DialogTitle>知识整理记录</DialogTitle><DialogDescription>按当前数据源列出任务，生成范围以各任务提交时为准。</DialogDescription></DialogHeader>{jobs.error ? <Failure error={jobs.error} retry={jobs.retry} /> : !jobItems.length && jobs.loading ? <Loading /> : !jobItems.length ? <p className="py-10 text-center text-sm text-muted-foreground">暂无知识整理记录。</p> : <div className="space-y-3">{jobItems.map(job => <div key={job.id} className="rounded-lg border p-4"><div className="flex flex-wrap items-center justify-between gap-2 text-sm"><span className="font-medium">任务 #{job.id} · 来源问题 #{job.topic_id}</span><Badge variant="outline" className="font-normal">{jobLabels[job.status]}</Badge></div><p className="mt-2 text-xs text-muted-foreground">{job.created_at || '时间未知'} · {job.model} · {fmt(job.usage_tokens)} tokens</p><p className="mt-2 text-sm text-muted-foreground">成功覆盖 {fmt(job.processed)}/{fmt(job.total)} · 失败 {fmt(job.failed)} · 跳过 {fmt(job.skipped)}</p>{job.error && <p className="mt-2 whitespace-pre-wrap text-sm text-destructive">{job.error}</p>}{job.knowledge_id && <Button size="sm" variant="outline" className="mt-3" onClick={() => { setJobHistoryOpen(false); setSelectedId(job.knowledge_id!) }}>审核知识草稿<ArrowUpRight /></Button>}</div>)}</div>}<DialogFooter><Button variant="outline" onClick={jobs.retry}><RefreshCw />刷新记录</Button><Button onClick={() => setJobHistoryOpen(false)}>完成</Button></DialogFooter></DialogContent></Dialog>
  </div>
}

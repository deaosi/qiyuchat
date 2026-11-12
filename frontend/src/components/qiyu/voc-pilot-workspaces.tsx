import { useRef, useState, type ReactNode } from 'react'
import { ArrowUpRight, ChevronLeft, ChevronRight, Copy } from 'lucide-react'
import { toast } from 'sonner'
import { normalizePair, type VocCasePath, type VocCaseSolution, type VocIssuePilot, type VocPilotCase, type VocPilotEvidence, type VocPilotFamily, type VocPilotIssue, type VocPlaybook, type VocPlaybookBranch } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import type { OpenEvidencePair } from './voc-next-workspace'
import { displayTime } from './voc-topic-evidence'

const families: Record<VocPilotFamily, string> = {
  no_reply: '机器人不回复', scenario_config: '场景配置', configuration: '配置与使用', anomaly: '功能异常',
  platform_limit: '能力与限制咨询', other: '其他咨询',
}
const issueStatuses: Record<VocPilotIssue['evidence_status'], string> = {
  reported: '客户反馈', staff_investigated: '客服已排查', cause_confirmed: '原因已确认',
}
const playbookStatuses: Record<VocPlaybook['status'], string> = {
  investigation: '待沉淀', guidance: '已核验指引', verified: '效果已验证',
}
const gapLabels: Record<string, string> = {
  applicability: '适用条件', case_evidence: '完整处理记录', complete_procedure: '完整操作步骤',
  post_change_retest: '操作后复测', verified_outcome: '复测结果', conflicting_case: '与其他案例不一致的结果',
  platform: '平台', module: '功能模块', condition: '触发条件', staff_reply: '人工回复', root_cause: '原因确认',
}
const count = new Intl.NumberFormat('zh-CN').format
const issuePageSize = 6
const playbookPageSize = 8

function scopeText(item: { platform: string; module: string; condition: string }) {
  return [item.platform, item.module, item.condition].filter(Boolean).join(' · ')
}

function categoryText(item: Pick<VocPilotIssue, 'family' | 'platform' | 'module' | 'condition'>) {
  return [families[item.family], scopeText(item)].filter(Boolean).join(' · ')
}

function matchSearch(search: string, fields: string[]) {
  const terms = search.normalize('NFKC').toLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return true
  const text = fields.join('\n').normalize('NFKC').toLowerCase()
  return terms.every(term => text.includes(term))
}

function MissingEvidence({ values }: { values: string[] }) {
  const labels = [...new Set(values.map(value => gapLabels[value] ?? (/^[a-z][a-z0-9_]*$/i.test(value) ? '其他证据' : value)))].filter(Boolean)
  if (!labels.length) return null
  return <p className="text-sm leading-6 text-muted-foreground"><span className="mr-2 font-medium text-foreground">待补证据</span>{labels.join('、')}</p>
}

function openSource(source: VocPilotEvidence, onOpenPair: OpenEvidencePair, anchored = true) {
  onOpenPair(normalizePair({ qa_id: source.qa_id, question: source.quote, asked_at: source.ts }), anchored ? source.message_id : undefined)
}

function SourceLine({ source, onOpenPair, anchored = true, label, children }: { source: VocPilotEvidence; onOpenPair: OpenEvidencePair; anchored?: boolean; label?: string; children?: ReactNode }) {
  const hasAnchor = anchored && Boolean(source.message_id.trim())
  return <div className="min-w-0 space-y-1 py-2">
    {label && <p className="text-xs text-muted-foreground">{label}</p>}
    <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{source.quote}</p>
    {children}
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      {source.ts > 0 && <span className="tabular-nums">{displayTime(source.ts)}</span>}
      <Button type="button" variant="ghost" size="xs" onClick={() => openSource(source, onOpenPair, hasAnchor)}>{hasAnchor ? '定位原话' : '查看会话'}<ArrowUpRight /></Button>
    </div>
  </div>
}

const detailFields = [['goal', '咨询目标'], ['observed', '实际现象'], ['checked', '已做检查']] as const

function caseSearchFields(cases: VocPilotCase[]) {
  return cases.flatMap(source => [source.quote, ...detailFields.map(([key]) => source.details?.[key] ?? '')])
}

function CaseDetails({ source }: { source: VocPilotCase }) {
  const shown = new Set([source.quote.trim()])
  const fields = detailFields.flatMap(([key, label]) => {
    const value = source.details?.[key]?.trim()
    if (!value || shown.has(value)) return []
    shown.add(value)
    return [{ key, label, value }]
  })
  if (!fields.length) return null
  return <dl className="grid gap-x-3 gap-y-1 py-2 text-sm leading-6 sm:grid-cols-[auto_minmax(0,1fr)]">{fields.map(({ key, label, value }) => <div key={key} className="contents">
    <dt className="text-muted-foreground">{label}</dt><dd className="whitespace-pre-wrap [overflow-wrap:anywhere]">{value}</dd>
  </div>)}</dl>
}

function CaseRecords({ cases, onOpenPair }: { cases: VocPilotCase[]; onOpenPair: OpenEvidencePair }) {
  if (!cases.length) return null
  return <section><div className="mb-1 flex flex-wrap items-center gap-x-2"><h3 className="text-sm font-medium">客户咨询摘录</h3>
    <span className="text-xs text-muted-foreground">敏感信息已隐藏</span></div><div className="divide-y">{cases.map((source, index) =>
    <SourceLine key={`${source.qa_id}-${source.message_id}-${index}`} source={source} anchored={source.anchored} onOpenPair={onOpenPair} label={source.has_reply ? '已采集到客服回复' : undefined}>
      <CaseDetails source={source} />
    </SourceLine>)}</div></section>
}

function ImpactCounts({ impact, checkedCases = 0 }: { impact?: VocPilotIssue['impact']; checkedCases?: number }) {
  if (!impact && !checkedCases) return null
  return <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm tabular-nums">
    {impact && <><span>{count(impact.consultations)} 次咨询</span><span>{count(impact.sessions)} 个会话</span><span>识别 {count(impact.merchants)} 家商家</span>
      {impact.unidentified_sessions > 0 && <span className="text-muted-foreground">{count(impact.unidentified_sessions)} 个会话身份待核实</span>}
    </>}
    {checkedCases > 0 && <span>{count(checkedCases)} 个关联处理案例</span>}
  </div>
}

function PageNav({ label, current, last, onChange }: { label: string; current: number; last: number; onChange: (page: number) => void }) {
  if (!last) return null
  return <nav aria-label={label} className="flex flex-wrap items-center justify-end gap-2 border-t pt-3 text-xs text-muted-foreground">
    <Button type="button" variant="outline" size="sm" disabled={current === 0} onClick={() => onChange(current - 1)}><ChevronLeft />上一页</Button>
    <span aria-live="polite" className="tabular-nums">第 {current + 1} / {last + 1} 页</span>
    <Button type="button" variant="outline" size="sm" disabled={current === last} onClick={() => onChange(current + 1)}>下一页<ChevronRight /></Button>
  </nav>
}

export function VocIssuePilotResults({ data, onOpenPair }: { data: VocIssuePilot; onOpenPair: OpenEvidencePair }) {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const opener = useRef<HTMLButtonElement | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const matches = data.items.filter(item => matchSearch(search, [item.title, categoryText(item), ...caseSearchFields(item.cases)]))
  const last = Math.max(0, Math.ceil(matches.length / issuePageSize) - 1)
  const current = Math.min(page, last)
  const selected = data.items.find(item => item.id === selectedId)
  const coverage = data.coverage
  return <div className="space-y-3">
    <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums text-muted-foreground">
      <span>读取 {count(coverage.source_events)} 条咨询片段</span><span>命中 {count(coverage.matched_consultations)} 次咨询</span>
      <span>已归类 {count(coverage.classified_consultations)} 次</span><span>待补上下文 {count(coverage.needs_context)} 次</span>
    </p>
    {data.items.length > 0 && <Input ref={input} type="search" value={search} maxLength={200} aria-label="搜索产品问题" placeholder="搜索问题、平台或触发条件"
      className="w-full sm:max-w-xs" onChange={event => { setSearch(event.target.value); setPage(0) }} />}
    {matches.length ? <ul className="divide-y rounded-md border px-2 sm:px-3">{matches.slice(current * issuePageSize, (current + 1) * issuePageSize).map(item =>
      <li key={item.id}>
        <button type="button" className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-x-4 gap-y-1 rounded-sm px-2 py-3 text-left hover:bg-muted/35 focus-visible:outline-2 focus-visible:outline-ring"
          onClick={event => { opener.current = event.currentTarget; setSelectedId(item.id) }}>
          <span className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
            <span className="flex flex-wrap items-center gap-2"><span className="line-clamp-2 font-medium">{item.cases[0]?.quote ?? item.title}</span><Badge variant="outline" className="font-normal">{issueStatuses[item.evidence_status]}</Badge>
              {item.scope_status === 'needs_context' && <Badge variant="outline" className="font-normal">范围待补</Badge>}
            </span>
            <span className="block text-xs text-muted-foreground">{[item.cases.length ? item.title : '', categoryText(item)].filter(Boolean).join(' · ')}</span>
          </span>
          <span className="flex items-center gap-2 text-right text-xs tabular-nums"><span>{count(item.impact.consultations)} 次咨询</span><ChevronRight aria-hidden="true" className="size-4 text-muted-foreground" /></span>
        </button>
      </li>)}</ul> : <p className="py-5 text-center text-sm text-muted-foreground">{search.trim() ? '未找到匹配的产品问题' : '当前范围暂无可归类的产品问题'}</p>}
    <PageNav label="产品问题分页" current={current} last={last} onChange={setPage} />
    <Sheet open={Boolean(selected)} onOpenChange={open => { if (!open) setSelectedId(null) }}>
      <SheetContent className="w-full gap-0 sm:max-w-2xl" onCloseAutoFocus={event => { event.preventDefault(); (opener.current?.isConnected ? opener.current : input.current)?.focus() }}>
        {selected && <>
          <SheetHeader className="shrink-0 gap-2 border-b px-5 py-5 pr-10">
            <div className="flex flex-wrap gap-2"><Badge variant="outline" className="font-normal">{issueStatuses[selected.evidence_status]}</Badge>
              {selected.scope_status === 'needs_context' && <Badge variant="outline" className="font-normal">范围待补</Badge>}
            </div>
            <SheetTitle className="text-lg [overflow-wrap:anywhere]">{selected.title}</SheetTitle>
            <SheetDescription>{categoryText(selected)}</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5">
            <CaseRecords cases={selected.cases} onOpenPair={onOpenPair} />
            <ImpactCounts impact={selected.impact} />
            <section className="space-y-2 border-t pt-3">
              <MissingEvidence values={selected.next.missing} />
              <Button asChild variant="outline" size="sm"><a href={`#voc-next?issue=${encodeURIComponent(selected.id)}`}>查看处理手册<ArrowUpRight /></a></Button>
            </section>
          </div>
        </>}
      </SheetContent>
    </Sheet>
  </div>
}

function visibleBranches(item: VocPlaybook) {
  if (item.status === 'investigation') return []
  return item.branches.filter(branch => branch.steps.length > 0
    && branch.steps.every(step => step.text.trim() && step.evidence.length > 0)
    && branch.retest.trim() && branch.retest_evidence?.length)
}

function branchStatus(item: VocPlaybook, branch: VocPlaybookBranch) {
  return (branch.status ?? item.status) === 'verified' && branch.result_evidence?.length ? 'verified' : 'guidance'
}

function PlaybookBadge({ item, branches }: { item: VocPlaybook; branches: VocPlaybookBranch[] }) {
  let label = item.checked_cases === 0 && item.cases?.length ? '仅有咨询记录' : playbookStatuses.investigation
  const verified = item.status === 'verified' && branches.length > 0 && branches.every(branch => branchStatus(item, branch) === 'verified')
  if (branches.length > 0) label = verified ? playbookStatuses.verified : playbookStatuses.guidance
  return <Badge variant={verified ? 'default' : 'outline'} className="w-fit font-normal">{label}</Badge>
}

async function copyBranch(playbook: VocPlaybook, branch: VocPlaybookBranch, condition: string, escalation: string) {
  const text = [playbook.title, playbookStatuses[branchStatus(playbook, branch)], scopeText(playbook), condition,
    ...branch.steps.map((step, i) => `${i + 1}. ${step.text}`), `复测 ${branch.retest}`,
    escalation && `升级处理 ${escalation}`].filter(Boolean).join('\n')
  try { await navigator.clipboard.writeText(text); toast.success('已复制处理步骤。') }
  catch { toast.error('剪贴板暂不可用，可选中文字复制。') }
}

function EvidenceDetails({ title, sources, onOpenPair }: { title: string; sources?: VocPilotEvidence[]; onOpenPair: OpenEvidencePair }) {
  if (!sources?.length) return null
  return <details className="text-xs text-muted-foreground"><summary className="w-fit cursor-pointer">{title}</summary>
    {sources.map((source, index) => <SourceLine key={`${source.qa_id}-${source.message_id}-${index}`} source={source} onOpenPair={onOpenPair} />)}
  </details>
}

function Branch({ item, branch, onOpenPair }: { item: VocPlaybook; branch: VocPlaybookBranch; onOpenPair: OpenEvidencePair }) {
  const status = branchStatus(item, branch)
  const condition = branch.condition_evidence?.length ? branch.condition : ''
  const escalation = branch.escalation_evidence?.length ? branch.escalation : ''
  return <section className="min-w-0 space-y-3 rounded-md border p-3">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <h4 className="text-sm font-medium [overflow-wrap:anywhere]">{condition || '处理步骤'}</h4>
      <div className="flex flex-wrap items-center gap-2"><Badge variant="outline" className="font-normal">{playbookStatuses[status]}</Badge>
        {branch.case_count !== undefined && <span className="text-xs tabular-nums text-muted-foreground">{count(branch.case_count)} 个案例</span>}
        <Button type="button" variant="outline" size="xs" onClick={() => void copyBranch(item, branch, condition, escalation)}><Copy />复制处理步骤</Button>
      </div>
    </div>
    <EvidenceDetails title="适用条件依据" sources={branch.condition_evidence} onOpenPair={onOpenPair} />
    <ol className="ml-5 list-decimal space-y-2 text-sm leading-6">
      {branch.steps.map((step, i) => <li key={i} className="min-w-0 [overflow-wrap:anywhere]">
        {step.text}
        <EvidenceDetails title="步骤依据" sources={step.evidence} onOpenPair={onOpenPair} />
      </li>)}
    </ol>
    <p className="text-sm leading-6 [overflow-wrap:anywhere]"><span className="mr-2 font-medium">复测</span>{branch.retest}</p>
    <EvidenceDetails title="复测依据" sources={branch.retest_evidence} onOpenPair={onOpenPair} />
    <EvidenceDetails title="客户反馈依据" sources={branch.result_evidence} onOpenPair={onOpenPair} />
    {escalation && <p className="text-sm leading-6 [overflow-wrap:anywhere]"><span className="mr-2 font-medium">升级处理</span>{escalation}</p>}
    <EvidenceDetails title="升级处理依据" sources={branch.escalation_evidence} onOpenPair={onOpenPair} />
  </section>
}

function linkedIssueFromHash() {
  const [route, query = ''] = window.location.hash.split('?')
  if (route !== '#voc-next') return null
  const issue = new URLSearchParams(query).get('issue')
  return issue && /^issue_[a-f0-9]{24}$/.test(issue) ? issue : null
}

const caseStepLabels: Record<VocCasePath['steps'][number]['state'], string> = {
  proposed: '提出建议', reported: '说明处理情况', instructed: '给出操作步骤',
}

const solutionSectionLabels: Record<VocCaseSolution['sections'][number]['kind'], string> = {
  problem: '客户问题', diagnosis: '排查判断', action: '处理操作', decision: '处理决定', verification: '验证情况',
}

function CaseSolution({ solution, onOpenPair }: { solution: VocCaseSolution; onOpenPair: OpenEvidencePair }) {
  return <div className="divide-y">{solution.sections.map((section, index) =>
    <section className="space-y-1 py-3 first:pt-0" key={`${section.kind}-${index}`}>
      <h3 className="text-sm font-medium">{solutionSectionLabels[section.kind]}</h3>
      <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{section.text}</p>
      <div className="flex flex-wrap gap-x-2">{section.evidence.map((source, sourceIndex) =>
        <Button key={`${source.qa_id}-${source.message_id}-${sourceIndex}`} type="button" variant="ghost" size="xs" onClick={() => openSource(source, onOpenPair)}>
          {section.evidence.length === 1 ? '定位原话' : `定位原话 ${sourceIndex + 1}`}<ArrowUpRight />
        </Button>)}</div>
    </section>)}</div>
}

function CasePathSource({ item, onOpenPair }: { item: VocCasePath; onOpenPair: OpenEvidencePair }) {
  return <>
    <section>
      <h3 className="text-sm font-medium">客户咨询</h3>
      <SourceLine source={item.problem} onOpenPair={onOpenPair} />
    </section>
    {item.steps.length > 0 && <section className="border-t pt-4">
      <h3 className="text-sm font-medium">处理经过</h3>
      <ol className="divide-y">{item.steps.map((step, index) => {
        const actor = step.actor === 'staff' ? '客服' : '客户'
        const label = `${actor} · ${caseStepLabels[step.state]}`
        return <li key={`${step.evidence.message_id}-${index}`}>
          {step.action === step.evidence.quote ? <SourceLine source={step.evidence} onOpenPair={onOpenPair} label={label} />
            : <div className="space-y-1 py-2">
              <p className="text-xs text-muted-foreground">{label}</p>
              <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{step.action}</p>
              <EvidenceDetails title={`查看${actor}原话`} sources={[step.evidence]} onOpenPair={onOpenPair} />
            </div>}
        </li>
      })}</ol>
    </section>}
    {item.criterion && <section className="border-t pt-4">
      <h3 className="text-sm font-medium">客户期望</h3>
      <SourceLine source={item.criterion} onOpenPair={onOpenPair} />
    </section>}
  </>
}

function CasePathDetail({ item, onOpenPair }: { item: VocCasePath; onOpenPair: OpenEvidencePair }) {
  return <>
    <SheetHeader className="shrink-0 gap-2 border-b px-5 py-5 pr-10">
      <SheetTitle className="text-lg [overflow-wrap:anywhere]">{item.solution?.title ?? '案例处理记录'}</SheetTitle>
      <SheetDescription className={item.platform ? undefined : 'sr-only'}>{item.platform || '查看客户咨询和会话中的处理经过。'}</SheetDescription>
    </SheetHeader>
    <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-5">
      {item.solution ? <CaseSolution solution={item.solution} onOpenPair={onOpenPair} /> : <CasePathSource item={item} onOpenPair={onOpenPair} />}
      {!item.solution && <p className="border-t pt-3 text-sm text-muted-foreground">未记录复测结果</p>}
      {item.solution && <details className="border-t pt-3 text-sm">
        <summary className="w-fit cursor-pointer font-medium">{item.steps.length > 0 ? '原始处理记录' : '原始咨询'}</summary>
        <div className="mt-3 space-y-4"><CasePathSource item={item} onOpenPair={onOpenPair} /></div>
      </details>}
    </div>
  </>
}

export function VocPlaybookResults({ items, casePaths = [], onOpenPair }: { items: VocPlaybook[]; casePaths?: VocCasePath[]; onOpenPair: OpenEvidencePair }) {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [selection, setSelection] = useState<{ kind: 'issue' | 'case'; id: string } | null>(null)
  const [linkedIssue, setLinkedIssue] = useState(linkedIssueFromHash)
  const opener = useRef<HTMLButtonElement | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const matches = items.map(item => ({ item, branches: visibleBranches(item) })).filter(({ item, branches }) => matchSearch(search, [item.title, categoryText(item),
    ...(item.cases === undefined ? item.evidence.map(source => source.quote) : caseSearchFields(item.cases)),
    ...branches.flatMap(branch => [branch.retest, ...branch.steps.map(step => step.text),
      branch.condition_evidence?.length ? branch.condition : '', branch.escalation_evidence?.length ? branch.escalation : ''])]))
  const matchingPaths = casePaths.filter(item => matchSearch(search, [item.platform, item.problem.quote, item.criterion?.quote ?? '',
    ...item.steps.map(step => step.evidence.quote), item.solution?.title ?? '', ...(item.solution?.sections.map(section => section.text) ?? [])]))
  const last = Math.max(0, Math.ceil((matchingPaths.length + matches.length) / playbookPageSize) - 1)
  const current = Math.min(page, last)
  const offset = current * playbookPageSize
  const pathsOnPage = matchingPaths.slice(offset, offset + playbookPageSize)
  const issueOffset = Math.max(0, offset - matchingPaths.length)
  const issuesOnPage = matches.slice(issueOffset, issueOffset + playbookPageSize - pathsOnPage.length)
  const selectedPath = selection?.kind === 'case' ? casePaths.find(item => item.id === selection.id) : undefined
  const selected = selection?.kind === 'case' ? undefined : items.find(item => selection ? item.id === selection.id : item.issue_id === linkedIssue)
  const selectedBranches = selected ? visibleBranches(selected) : []
  return <div className="space-y-3">
    {(items.length > 0 || casePaths.length > 0) && <Input ref={input} type="search" maxLength={200} value={search} aria-label="搜索处理手册" placeholder="搜索客户咨询、适用条件或步骤"
      className="w-full sm:max-w-sm" onChange={event => { setSearch(event.target.value); setPage(0) }} />}
    {pathsOnPage.length > 0 && <section className="space-y-2">
      <h3 className="text-sm font-medium">已有处理记录的案例</h3>
      <ul className="divide-y rounded-md border px-2 sm:px-3">{pathsOnPage.map(item => <li key={item.id}>
        <button type="button" className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-3 rounded-sm px-2 py-3 text-left hover:bg-muted/35 focus-visible:outline-2 focus-visible:outline-ring"
          onClick={event => { opener.current = event.currentTarget; setSelection({ kind: 'case', id: item.id }) }}>
          <span className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
            <span className="line-clamp-2 font-medium">{item.solution?.title ?? item.problem.quote}</span>
            <span className="line-clamp-1 text-sm text-muted-foreground">{item.solution?.sections.find(section => section.kind === 'action')?.text ?? item.steps[0]?.action}</span>
            <span className="block text-xs text-muted-foreground">{[item.platform, item.problem.ts > 0 ? displayTime(item.problem.ts) : ''].filter(Boolean).join(' · ')}</span>
          </span>
          <ChevronRight aria-hidden="true" className="mt-1 size-4 text-muted-foreground" />
        </button>
      </li>)}</ul>
    </section>}
    {issuesOnPage.length > 0 && <section className="space-y-2">
      {matchingPaths.length > 0 && <h3 className="text-sm font-medium">问题汇总</h3>}
      <ul className="divide-y rounded-md border px-2 sm:px-3">{issuesOnPage.map(({ item, branches }) =>
      <li key={item.id}>
        <button type="button" className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-3 rounded-sm px-2 py-3 text-left hover:bg-muted/35 focus-visible:outline-2 focus-visible:outline-ring"
          onClick={event => { opener.current = event.currentTarget; setSelection({ kind: 'issue', id: item.id }) }}>
          <span className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
            <span className="flex flex-wrap items-center gap-2"><span className="line-clamp-2 font-medium">{item.cases?.[0]?.quote ?? item.title}</span><PlaybookBadge item={item} branches={branches} /></span>
            <span className="block text-xs text-muted-foreground">{[item.cases?.length ? item.title : '', categoryText(item)].filter(Boolean).join(' · ')}</span>
            {(item.impact || item.checked_cases > 0) && <span className="block text-xs tabular-nums text-muted-foreground">{[
              item.impact ? `${count(item.impact.consultations)} 次咨询` : '',
              item.checked_cases > 0 ? `${count(item.checked_cases)} 个关联处理案例` : '',
            ].filter(Boolean).join(' · ')}</span>}
          </span>
          <ChevronRight aria-hidden="true" className="mt-1 size-4 text-muted-foreground" />
        </button>
    </li>)}</ul></section>}
    {matchingPaths.length === 0 && matches.length === 0 && <p className="py-8 text-center text-sm text-muted-foreground">{search.trim() ? '未找到匹配的咨询或处理流程' : '当前范围暂无可查看的咨询线索'}</p>}
    <PageNav label="处理手册分页" current={current} last={last} onChange={setPage} />
    <Sheet open={Boolean(selected || selectedPath)} onOpenChange={open => {
      if (open) return
      setSelection(null)
      setLinkedIssue(null)
      if (linkedIssueFromHash()) history.replaceState(null, '', '#voc-next')
    }}>
      <SheetContent className="w-full gap-0 sm:max-w-2xl" onCloseAutoFocus={event => { event.preventDefault(); (opener.current?.isConnected ? opener.current : input.current)?.focus() }}>
        {selectedPath ? <CasePathDetail item={selectedPath} onOpenPair={onOpenPair} /> : selected && <>
          <SheetHeader className="shrink-0 gap-2 border-b px-5 py-5 pr-10">
            <PlaybookBadge item={selected} branches={selectedBranches} />
            <SheetTitle className="text-lg [overflow-wrap:anywhere]">{selectedBranches.length > 0 ? selected.title : '咨询详情'}</SheetTitle>
            <SheetDescription>{[selectedBranches.length ? '' : selected.title, categoryText(selected)].filter(Boolean).join(' · ')}</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5">
            <CaseRecords cases={selected.cases ?? []} onOpenPair={onOpenPair} />
            <EvidenceDetails title="会话依据" sources={selected.cases === undefined ? selected.evidence : undefined} onOpenPair={onOpenPair} />
            <ImpactCounts impact={selected.impact} checkedCases={selected.checked_cases} />
            {selectedBranches.length > 0 && <section className="space-y-2"><h3 className="text-sm font-medium">有会话依据的处理流程</h3>
              {selectedBranches.map((branch, index) => <Branch key={branch.id ?? index} item={selected} branch={branch} onOpenPair={onOpenPair} />)}
            </section>}
            <MissingEvidence values={selected.missing} />
            <EvidenceDetails title="结果不一致的会话依据" sources={selected.counterevidence} onOpenPair={onOpenPair} />
          </div>
        </>}
      </SheetContent>
    </Sheet>
  </div>
}

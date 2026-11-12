import { useRef, useState } from 'react'
import { ArrowUpRight, ChevronLeft, ChevronRight, Copy } from 'lucide-react'
import { toast } from 'sonner'
import { normalizePair, type VocIssuePilot, type VocPilotEvidence, type VocPilotFamily, type VocPilotIssue, type VocPlaybook, type VocPlaybookBranch } from '@/lib/api'
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
  investigation: '排查指南', guidance: '已核验指引', verified: '效果已验证',
}
const owners: Record<string, string> = { support: '客服', product: '产品', engineering: '研发', merchant: '商家' }
const gapLabels: Record<string, string> = {
  applicability: '适用条件', case_evidence: '完整处理记录', complete_procedure: '完整操作步骤',
  post_change_retest: '操作后复测', verified_outcome: '复测结果', conflicting_case: '与其他案例不一致的结果',
  platform: '平台', module: '功能模块', condition: '触发条件', staff_reply: '人工回复', root_cause: '原因确认',
}
const count = new Intl.NumberFormat('zh-CN').format
const issuePageSize = 6
const playbookPageSize = 8

function ownerLabel(owner: string) {
  return owners[owner] ?? owner.trim()
}

function scopeText(item: { platform: string; module: string; condition: string }) {
  return [item.platform, item.module, item.condition].filter(Boolean).join(' · ')
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

function SourceLine({ source, onOpenPair, anchored = true, label }: { source: VocPilotEvidence; onOpenPair: OpenEvidencePair; anchored?: boolean; label?: string }) {
  const open = () => onOpenPair(normalizePair({ qa_id: source.qa_id, question: source.quote, asked_at: source.ts }), anchored ? source.message_id : undefined)
  return <div className="min-w-0 space-y-1 py-2">
    {label && <p className="text-xs text-muted-foreground">{label}</p>}
    <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{source.quote}</p>
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      {source.ts > 0 && <span className="tabular-nums">{displayTime(source.ts)}</span>}
      <Button type="button" variant="ghost" size="xs" onClick={open}>{anchored ? '定位原话' : '查看会话'}<ArrowUpRight /></Button>
    </div>
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
  const matches = data.items.filter(item => matchSearch(search, [item.title, families[item.family], scopeText(item), item.next.action]))
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
            <span className="flex flex-wrap items-center gap-2"><span className="font-medium">{item.title}</span><Badge variant="outline" className="font-normal">{issueStatuses[item.evidence_status]}</Badge>
              {item.scope_status === 'needs_context' && <Badge variant="outline" className="font-normal">范围待补</Badge>}
            </span>
            <span className="block text-xs text-muted-foreground">{[families[item.family], scopeText(item)].filter(Boolean).join(' · ')}</span>
            {item.next.action && <span className="line-clamp-2 block text-sm leading-5 text-muted-foreground"><span className="mr-2 text-foreground">{ownerLabel(item.next.owner) || '下一步'}</span>{item.next.action}</span>}
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
            <SheetDescription>{[families[selected.family], scopeText(selected)].filter(Boolean).join(' · ')}</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5">
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm tabular-nums"><span>{count(selected.impact.consultations)} 次咨询</span><span>{count(selected.impact.sessions)} 个会话</span><span>识别 {count(selected.impact.merchants)} 家商家</span>
              {selected.impact.unidentified_sessions > 0 && <span className="text-muted-foreground">{count(selected.impact.unidentified_sessions)} 个会话身份待核实</span>}
            </div>
            <section className="space-y-2 rounded-md border bg-muted/20 p-3"><h3 className="text-sm font-medium">下一步</h3>
              <p className="text-sm leading-6 [overflow-wrap:anywhere]">{ownerLabel(selected.next.owner) && <span className="mr-2 text-muted-foreground">{ownerLabel(selected.next.owner)}</span>}{selected.next.action}</p>
              <MissingEvidence values={selected.next.missing} />
              <Button asChild variant="outline" size="sm"><a href={`#voc-next?issue=${encodeURIComponent(selected.id)}`}>查看处理手册<ArrowUpRight /></a></Button>
            </section>
            {selected.cases.length > 0 && <section><h3 className="mb-2 text-sm font-medium">会话依据</h3><div className="divide-y">{selected.cases.map((source, index) =>
              <SourceLine key={`${source.qa_id}-${source.message_id}-${index}`} source={source} anchored={source.anchored} onOpenPair={onOpenPair} label={source.has_reply ? '有回复' : '待补人工回复'} />)}</div></section>}
          </div>
        </>}
      </SheetContent>
    </Sheet>
  </div>
}

function canCopyBranch(playbook: VocPlaybook, branch: VocPlaybookBranch) {
  return playbook.status !== 'investigation' && branch.steps.length > 0
    && branch.steps.every(step => step.text.trim() && step.evidence.length > 0)
}

async function copyBranch(playbook: VocPlaybook, branch: VocPlaybookBranch) {
  if (!canCopyBranch(playbook, branch)) return
  const status = branch.status ?? playbook.status
  const text = [playbook.title, playbookStatuses[status], scopeText(playbook), branch.condition,
    ...branch.steps.map((step, i) => `${i + 1}. ${step.text}`), branch.retest && `复测 ${branch.retest}`,
    branch.escalation && `升级处理 ${branch.escalation}`].filter(Boolean).join('\n')
  try { await navigator.clipboard.writeText(text); toast.success('已复制处理步骤。') }
  catch { toast.error('剪贴板暂不可用，可选中文字复制。') }
}

function Branch({ item, branch, onOpenPair }: { item: VocPlaybook; branch: VocPlaybookBranch; onOpenPair: OpenEvidencePair }) {
  const status = branch.status ?? item.status
  return <section className="min-w-0 space-y-3 rounded-md border p-3">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <h4 className="text-sm font-medium [overflow-wrap:anywhere]">{branch.condition || '适用流程'}</h4>
      <div className="flex flex-wrap items-center gap-2"><Badge variant="outline" className="font-normal">{playbookStatuses[status]}</Badge>
        {branch.case_count !== undefined && <span className="text-xs tabular-nums text-muted-foreground">{count(branch.case_count)} 个案例</span>}
        {canCopyBranch(item, branch) && <Button type="button" variant="outline" size="xs" onClick={() => void copyBranch(item, branch)}><Copy />复制处理步骤</Button>}
      </div>
    </div>
    {!!branch.condition_evidence?.length && <details className="text-xs text-muted-foreground"><summary className="w-fit cursor-pointer">适用条件依据</summary>{branch.condition_evidence.map((source, j) => <SourceLine key={`${source.qa_id}-${source.message_id}-${j}`} source={source} onOpenPair={onOpenPair} />)}</details>}
    <ol className="ml-5 list-decimal space-y-2 text-sm leading-6">
      {branch.steps.map((step, i) => <li key={i} className="min-w-0 [overflow-wrap:anywhere]">
        {step.text}
        {step.evidence.length > 0 && <details className="text-xs text-muted-foreground"><summary className="w-fit cursor-pointer">步骤依据</summary>{step.evidence.map((source, j) => <SourceLine key={`${source.qa_id}-${source.message_id}-${j}`} source={source} onOpenPair={onOpenPair} />)}</details>}
      </li>)}
    </ol>
    {branch.retest && <p className="text-sm leading-6 [overflow-wrap:anywhere]"><span className="mr-2 font-medium">复测</span>{branch.retest}</p>}
    {!!branch.retest_evidence?.length && <details className="text-xs text-muted-foreground"><summary className="w-fit cursor-pointer">复测依据</summary>{branch.retest_evidence.map((source, j) => <SourceLine key={`${source.qa_id}-${source.message_id}-${j}`} source={source} onOpenPair={onOpenPair} />)}</details>}
    {!!branch.result_evidence?.length && <details className="text-xs text-muted-foreground"><summary className="w-fit cursor-pointer">客户反馈依据</summary>{branch.result_evidence.map((source, j) => <SourceLine key={`${source.qa_id}-${source.message_id}-${j}`} source={source} onOpenPair={onOpenPair} />)}</details>}
    {branch.escalation && <p className="text-sm leading-6 [overflow-wrap:anywhere]"><span className="mr-2 font-medium">升级处理</span>{branch.escalation}</p>}
    {!!branch.escalation_evidence?.length && <details className="text-xs text-muted-foreground"><summary className="w-fit cursor-pointer">升级处理依据</summary>{branch.escalation_evidence.map((source, j) => <SourceLine key={`${source.qa_id}-${source.message_id}-${j}`} source={source} onOpenPair={onOpenPair} />)}</details>}
  </section>
}

function linkedIssueFromHash() {
  const [route, query = ''] = window.location.hash.split('?')
  if (route !== '#voc-next') return null
  const issue = new URLSearchParams(query).get('issue')
  return issue && /^issue_[a-f0-9]{24}$/.test(issue) ? issue : null
}

export function VocPlaybookResults({ items, onOpenPair }: { items: VocPlaybook[]; onOpenPair: OpenEvidencePair }) {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [linkedIssue, setLinkedIssue] = useState(linkedIssueFromHash)
  const opener = useRef<HTMLButtonElement | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const matches = items.filter(item => matchSearch(search, [item.title, families[item.family], scopeText(item),
    ...item.triage.flatMap(row => [row.check, row.when_yes, row.when_no]),
    ...item.branches.flatMap(branch => [branch.condition, branch.retest, branch.escalation, ...branch.steps.map(step => step.text)])]))
  const last = Math.max(0, Math.ceil(matches.length / playbookPageSize) - 1)
  const current = Math.min(page, last)
  const selected = items.find(item => selectedId ? item.id === selectedId : item.issue_id === linkedIssue)
  return <div className="space-y-3">
    {items.length > 0 && <Input ref={input} type="search" maxLength={200} value={search} aria-label="搜索处理手册" placeholder="搜索问题、适用条件或步骤"
      className="w-full sm:max-w-sm" onChange={event => { setSearch(event.target.value); setPage(0) }} />}
    {matches.length ? <ul className="divide-y rounded-md border px-2 sm:px-3">{matches.slice(current * playbookPageSize, (current + 1) * playbookPageSize).map(item =>
      <li key={item.id}>
        <button type="button" className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-3 rounded-sm px-2 py-3 text-left hover:bg-muted/35 focus-visible:outline-2 focus-visible:outline-ring"
          onClick={event => { opener.current = event.currentTarget; setSelectedId(item.id) }}>
          <span className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
            <span className="flex flex-wrap items-center gap-2"><span className="font-medium">{item.title}</span><Badge variant={item.status === 'verified' ? 'default' : 'outline'} className="font-normal">{playbookStatuses[item.status]}</Badge></span>
            <span className="block text-xs text-muted-foreground">{[families[item.family], scopeText(item), `已核对 ${count(item.checked_cases)} 条处理记录`].filter(Boolean).join(' · ')}</span>
            {item.triage[0] && <span className="line-clamp-2 block text-sm leading-5 text-muted-foreground">{item.triage[0].check}</span>}
          </span>
          <ChevronRight aria-hidden="true" className="mt-1 size-4 text-muted-foreground" />
        </button>
      </li>)}</ul> : <p className="py-8 text-center text-sm text-muted-foreground">{search.trim() ? '未找到匹配的处理手册' : '当前范围暂无处理手册'}</p>}
    <PageNav label="处理手册分页" current={current} last={last} onChange={setPage} />
    <Sheet open={Boolean(selected)} onOpenChange={open => {
      if (open) return
      setSelectedId(null)
      setLinkedIssue(null)
      if (linkedIssueFromHash()) history.replaceState(null, '', '#voc-next')
    }}>
      <SheetContent className="w-full gap-0 sm:max-w-2xl" onCloseAutoFocus={event => { event.preventDefault(); (opener.current?.isConnected ? opener.current : input.current)?.focus() }}>
        {selected && <>
          <SheetHeader className="shrink-0 gap-2 border-b px-5 py-5 pr-10">
            <Badge variant={selected.status === 'verified' ? 'default' : 'outline'} className="w-fit font-normal">{playbookStatuses[selected.status]}</Badge>
            <SheetTitle className="text-lg [overflow-wrap:anywhere]">{selected.title}</SheetTitle>
            <SheetDescription>{[families[selected.family], scopeText(selected), `已核对 ${count(selected.checked_cases)} 条处理记录`].filter(Boolean).join(' · ')}</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5">
            {selected.triage.length > 0 && <section className="space-y-2"><h3 className="text-sm font-medium">排查决策</h3>
              <ol className="space-y-2">{selected.triage.map((row, i) => <li key={i} className="space-y-1 rounded-md border bg-muted/20 p-3 text-sm leading-6 [overflow-wrap:anywhere]">
                <p className="font-medium">{i + 1}. {row.check}</p><p><span className="mr-2 text-muted-foreground">是</span>{row.when_yes}</p><p><span className="mr-2 text-muted-foreground">否</span>{row.when_no}</p>
              </li>)}</ol>
            </section>}
            {selected.branches.length > 0 && <section className="space-y-2"><h3 className="text-sm font-medium">按条件处理</h3>
              {selected.branches.map((branch, index) => <Branch key={branch.id ?? index} item={selected} branch={branch} onOpenPair={onOpenPair} />)}
            </section>}
            <MissingEvidence values={selected.missing} />
            {selected.evidence.length > 0 && <details className="text-sm"><summary className="w-fit cursor-pointer font-medium">会话依据</summary>
              <div className="divide-y">{selected.evidence.map((source, i) => <SourceLine key={`${source.qa_id}-${source.message_id}-${i}`} source={source} onOpenPair={onOpenPair} />)}</div>
            </details>}
          </div>
        </>}
      </SheetContent>
    </Sheet>
  </div>
}

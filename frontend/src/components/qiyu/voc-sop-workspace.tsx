import { useCallback, useState } from 'react'
import { ArrowUpRight, Copy } from 'lucide-react'
import { toast } from 'sonner'
import { useApi } from '@/hooks/use-api'
import { useBackgroundRefresh } from '@/hooks/use-background-refresh'
import { normalizePair, type ApiFilters, type VocSop, type VocSopEvidence, type VocSopStep } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { QueryPanel, VocKnowledgeWorkspace, type OpenEvidencePair } from './voc-next-workspace'
import { displayTime } from './voc-topic-evidence'

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
const pageSize = 12

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

function EvidenceLine({ evidence, onOpen, showQuote = true }: { evidence: VocSopEvidence; onOpen: (ref: VocSopEvidence) => void; showQuote?: boolean }) {
  const time = displayTime(evidence.ts)
  return <div className="space-y-1">
    {showQuote && <p className="whitespace-pre-wrap break-words text-sm [overflow-wrap:anywhere]">{evidence.quote}</p>}
    <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
      {time && <span className="tabular-nums">{time}</span>}
      <Button type="button" variant="ghost" size="xs" onClick={() => onOpen(evidence)}>定位原话<ArrowUpRight /></Button>
    </div>
  </div>
}

function SopCard({ item, onOpenPair }: { item: VocSop; onOpenPair: OpenEvidencePair }) {
  const open = (ref: VocSopEvidence) => onOpenPair(normalizePair({
    qa_id: item.qa_id, question: item.problem.quote, asked_at: item.problem.ts,
  }), ref.message_id)
  const missing = [...new Set(item.missing.map(code => gaps[code] ?? '其他待核验信息'))]
  const sameProblem = item.title === item.problem.quote
  return <article className="min-w-0 space-y-4 rounded-lg border p-4 sm:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={item.status === 'verified' ? 'default' : 'secondary'}>{statuses[item.status]}</Badge>
          <span className="text-xs text-muted-foreground">{item.kind === 'configuration' ? '配置与使用' : '异常与排障'}</span>
        </div>
        <h3 className="break-words font-medium [overflow-wrap:anywhere]">{item.title}</h3>
        {sameProblem && <EvidenceLine evidence={item.problem} onOpen={open} showQuote={false} />}
      </div>
      {item.status !== 'incomplete' && reusableSteps(item).length > 0 && <Button type="button" size="sm" variant="outline" onClick={() => void copySop(item)}><Copy />复制 SOP</Button>}
    </div>
    {(!sameProblem || item.conditions.length > 0) && <div className="grid gap-4 border-t pt-4 sm:grid-cols-2">
      {!sameProblem && <section className="min-w-0 space-y-2"><h4 className="text-sm font-medium">问题现象</h4><EvidenceLine evidence={item.problem} onOpen={open} /></section>}
      {item.conditions.length > 0 && <section className="min-w-0 space-y-2"><h4 className="text-sm font-medium">适用条件</h4>
        {item.conditions.map((ref, index) => <EvidenceLine key={`${ref.message_id}-${index}`} evidence={ref} onOpen={open} />)}
      </section>}
    </div>}
    {item.steps.length > 0 && <section className="space-y-2"><h4 className="text-sm font-medium">处理流程</h4>
      <ol className="space-y-3">
        {item.steps.map((step, index) => <li key={`${step.evidence.message_id}-${index}`} className="flex min-w-0 gap-3 rounded-md border bg-muted/15 p-3">
          <span className="pt-0.5 text-xs tabular-nums text-muted-foreground" aria-hidden="true">{index + 1}.</span>
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex flex-wrap items-center gap-2 text-xs"><span className="font-medium">{phases[step.phase]}</span><span className="text-muted-foreground">{outcomes[step.outcome]}</span></div>
            <EvidenceLine evidence={step.evidence} onOpen={open} />
            {step.feedback && step.feedback.message_id !== item.result?.message_id && <div className="space-y-1 border-l pl-3 text-muted-foreground"><p className="text-xs">客户反馈</p><EvidenceLine evidence={step.feedback} onOpen={open} /></div>}
          </div>
        </li>)}
      </ol>
    </section>}
    {!!item.replies?.length && <section className="space-y-2"><h4 className="text-sm font-medium">人工回复记录</h4>
      {item.replies.map((ref, index) => <EvidenceLine key={`${ref.message_id}-${index}`} evidence={ref} onOpen={open} />)}
    </section>}
    {item.result && <section className="space-y-2 rounded-md border p-3"><h4 className="text-sm font-medium">复测结果</h4><EvidenceLine evidence={item.result} onOpen={open} /></section>}
    {missing.length > 0 && <p className="text-xs leading-5 text-muted-foreground">待补充 {missing.join('、')}</p>}
  </article>
}

export function VocSopWorkspace({ filters, refreshKey = 0, onOpenPair }: Props) {
  const [revision, setRevision] = useState(0)
  const [search, setSearch] = useState('')
  const [limit, setLimit] = useState(pageSize)
  const key = refreshKey + revision
  const sops = useApi<{ items: VocSop[]; total: number }>('voc2/sops', filters, key)
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  useBackgroundRefresh(refresh, false, sops.loading || sops.refreshing, key)
  const terms = search.normalize('NFKC').toLowerCase().split(/\s+/).filter(Boolean)
  const matches = (sops.data?.items ?? []).filter(item => {
    if (!terms.length) return true
    const content = [item.title, item.problem.quote, item.result?.quote, ...item.conditions.map(ref => ref.quote), ...(item.replies?.map(ref => ref.quote) ?? []),
      ...item.steps.flatMap(step => [step.evidence.quote, step.feedback?.quote])].join('\n').normalize('NFKC').toLowerCase()
    return terms.every(term => content.includes(term))
  }).sort((a, b) => rank[a.status] - rank[b.status])
  const reusable = sops.data?.items.filter(item => item.status !== 'incomplete').length

  return <Card>
    <CardHeader><CardTitle>问题处理 SOP</CardTitle><CardDescription>{sops.data && reusable !== undefined
      ? `${reusable} 条可复用 · ${sops.data.total - reusable} 条待补证据`
      : '按原始会话还原问题现象、排查、处理和复测。'}</CardDescription></CardHeader>
    <CardContent>
      <QueryPanel hasData={sops.data !== null} loading={sops.loading} error={sops.error} retry={sops.retry}>
        <Input type="search" value={search} onChange={event => { setSearch(event.target.value); setLimit(pageSize) }} maxLength={200}
          placeholder="搜索问题、条件或步骤" aria-label="搜索处理 SOP" className="mb-4 w-full sm:max-w-sm" />
        {matches.length ? <div className="space-y-4">
          {matches.slice(0, limit).map(item => <SopCard key={item.id} item={item} onOpenPair={onOpenPair} />)}
          {matches.length > limit && <Button type="button" variant="outline" size="sm" onClick={() => setLimit(value => value + pageSize)}>显示更多 SOP</Button>}
        </div> : <div className="flex min-h-44 flex-col items-center justify-center gap-2 text-center text-sm text-muted-foreground">
          <p className="font-medium text-foreground">{terms.length ? '未找到匹配的处理 SOP' : '当前范围暂无可展示的处理流程'}</p>
          {terms.length ? <Button type="button" variant="ghost" size="sm" onClick={() => setSearch('')}>清除搜索</Button>
            : <p>整理到同一问题的人工处理步骤后，会在这里显示。</p>}
        </div>}
      </QueryPanel>
    </CardContent>
  </Card>
}

export function VocKnowledgeLibrary(props: Props) {
  return <Tabs defaultValue="sop" className="gap-4">
    <TabsList aria-label="知识库内容"><TabsTrigger value="sop">处理 SOP</TabsTrigger><TabsTrigger value="answers">可复用答案</TabsTrigger></TabsList>
    <TabsContent value="sop"><VocSopWorkspace {...props} /></TabsContent>
    <TabsContent value="answers"><VocKnowledgeWorkspace {...props} /></TabsContent>
  </Tabs>
}

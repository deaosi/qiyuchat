import { useRef, useState } from 'react'
import { ArrowUpRight, Copy, MessagesSquare } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { normalizePair } from '@/lib/api'
import type { Evidence, Knowledge, KnowledgeReuse, OpenEvidencePair, Ref, TopicDetail } from './voc-next-workspace'
import { diagnosisLabels, displayTime, EvidenceList, KnowledgeContent, knowledgeReuse, knowledgeReuseLevel, messageIdentifier, questionAnchor } from './voc-topic-evidence'

export const trendLabels = { all: '全部', new: '新出现', rising: '明显上升', persistent: '持续高频' } as const
export type TrendFilter = keyof typeof trendLabels
export type TopicTrend = { status: Exclude<TrendFilter, 'all'> | 'stable' | 'unknown'; previous_consultation_count: number | null; delta: number | null }
type Period = { start: string; end: string; source_pairs: number; processed_pairs: number }
export type TrendComparisonData = { current: Period; previous: Period | null; comparable: boolean; note: string; last_synced_at: string }

const number = (value?: number) => value === undefined ? '—' : value.toLocaleString('zh-CN')
const gapLabels: Record<string, string> = {
  no_staff_reply: '缺少人工客服回复',
  unverified_staff_reply: '客服身份未核验',
  closure_unconfirmed: '缺少处理结果或后续反馈',
  conflicting_evidence: '证据冲突或后续仍有疑问',
  insufficient_diagnosis: '缺少诊断依据',
}
const reuseLabels: Record<KnowledgeReuse, string> = { direct: '可直接复用', reference: '参考步骤', history: '历史答复 · 结果未确认', blocked: '证据冲突或仍有疑问 · 暂停复用' }
const confirmed = new Set(['config_cause_confirmed', 'platform_limit_confirmed', 'defect_confirmed'])

export function trendBadgeText(trend?: TopicTrend) {
  if (!trend || trend.status === 'stable' || trend.status === 'unknown') return ''
  return trend.status === 'rising' && trend.delta && trend.delta > 0
    ? `${trendLabels.rising} +${number(trend.delta)}` : trendLabels[trend.status]
}

function periodLabel(period: Period) {
  if (!period.start || !period.end) return '当前查询范围'
  return period.start === period.end ? period.start : `${period.start} 至 ${period.end}`
}

export function TrendComparison({ comparison, currentConsultations, trend }: { comparison: TrendComparisonData; currentConsultations?: number; trend?: TopicTrend }) {
  const periods = [{ label: '本期', period: comparison.current, consultations: currentConsultations }]
  if (comparison.previous) periods.push({ label: '上期', period: comparison.previous, consultations: trend?.previous_consultation_count ?? undefined })
  return <div className="rounded-xl border bg-muted/25 px-3.5 py-3 text-xs leading-5 sm:px-4" aria-label="数据覆盖与对比">
    <dl className="flex flex-wrap gap-x-7 gap-y-2">
      {periods.map(({ label, period, consultations }) => <div key={label}>
        <dt className="font-medium">{label} <span className="ml-1 font-normal text-muted-foreground">{periodLabel(period)}</span></dt>
        <dd className="tabular-nums text-muted-foreground">{consultations !== undefined && `${number(consultations)} 次咨询 · `}已整理 {number(period.processed_pairs)} / {number(period.source_pairs)} 条问答</dd>
      </div>)}
      <div>
        <dt className="font-medium">七鱼最近同步</dt>
        <dd className="tabular-nums text-muted-foreground">{comparison.last_synced_at ? displayTime(comparison.last_synced_at) : '暂无记录'}</dd>
      </div>
    </dl>
    {comparison.note && <p className="mt-2 border-t pt-2 text-muted-foreground">{comparison.note}</p>}
  </div>
}

function exact(refs: (Ref | null | undefined)[]): Ref[] {
  const quotes = new Set<string>()
  return refs.filter((ref): ref is Ref => {
    if (!ref || ref.alignment !== 'exact' || !ref.quote.trim() || quotes.has(ref.quote)) return false
    quotes.add(ref.quote)
    return true
  })
}

function caseFacts(item: Evidence) {
  return [
    { label: '电商后台设置', refs: exact(item.workbench?.commerce_settings ?? []), missing: '待补查平台侧商品设置。' },
    { label: '知汇鸽场景条件', refs: exact(item.workbench?.scenario_conditions ?? []), missing: '待补查场景启用及命中条件。' },
    { label: '实际触发结果', refs: exact(item.workbench?.actual ?? [item.domain?.actual]), missing: '待补查同一条件下的实际回复。' },
  ]
}

function refAnchor(item: Evidence, ref: Ref) {
  const direct = ref.source_refs?.map(source => messageIdentifier(source?.message_id)).find(Boolean)
  if (direct) return direct
  // For older references without coordinates, use a single unambiguous source.
  if (ref.source === 'question') {
    const ids = [...new Set((item.question_message_ids ?? []).map(messageIdentifier).filter(Boolean))]
    return ids.length === 1 ? ids[0] : undefined
  }
  const matches = item.claims.filter(claim => claim.start <= ref.start && claim.end >= ref.end)
  const ids = [...new Set(matches.flatMap(claim => claim.source_refs.map(source => messageIdentifier(source?.message_id))).filter(Boolean))]
  return ids.length === 1 ? ids[0] : undefined
}

function FactQuotes({ item, refs, onOpenPair }: { item: Evidence; refs: Ref[]; onOpenPair: OpenEvidencePair }) {
  return <ul className="space-y-2">{refs.map(ref => {
    const anchor = refAnchor(item, ref)
    const source = ref.source === 'question' ? '商家原话' : item.staff_source_verified ? '已核验客服原话' : '回复原话 · 来源未核验'
    return <li key={`${ref.source}-${ref.start}-${ref.end}`} className="min-w-0 space-y-1">
      <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{ref.quote}</p>
      <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
        <span>{source}</span>
        {item.qa_id && anchor && <button type="button" className="inline-flex min-h-6 items-center gap-1 text-foreground underline decoration-border underline-offset-2 hover:decoration-foreground focus-visible:outline-2" onClick={() => onOpenPair(normalizePair(item), anchor)}>定位原话<ArrowUpRight aria-hidden="true" className="size-3" /></button>}
      </div>
    </li>
  })}</ul>
}

function copyText(value: string, success: string) {
  void (async () => {
    try {
      await navigator.clipboard.writeText(value)
      toast.success(success)
    } catch {
      toast.error('剪贴板暂不可用，可选中文字复制。')
    }
  })()
}

export function copyKnowledge(item: Knowledge) {
  copyText([
    item.question, item.answer,
    item.scope && `适用范围 ${item.scope}`,
    ...item.steps.map((step, index) => `${index + 1}. ${step}`),
    item.cautions.length > 0 && `注意事项 ${item.cautions.join('；')}`,
  ].filter(Boolean).join('\n'), '已复制处理办法。')
}

function DecisionCase({ item, title, onOpenPair }: { item: Evidence; title: string; onOpenPair: OpenEvidencePair }) {
  const domain = item.domain
  const status = domain?.diagnosis?.status
  const facts = caseFacts(item)
  const cause = exact([domain?.diagnosis?.cause, ...(domain?.diagnosis?.evidence ?? [])])
  const steps = exact((domain?.actions ?? []).map(action => action.instruction).filter(ref => ref.source === 'answer' && ref.role === 'staff'))
  const verifiedSteps = item.staff_source_verified ? steps : []
  const suspected = item.workbench?.suspected_report_ready === true && item.staff_source_verified
    && domain?.primary_intent === 'functional_anomaly' && (status === 'reported' || status === 'pending') && facts.every(fact => fact.refs.length > 0)
  const diagnosis = status && confirmed.has(status) ? diagnosisLabels[status]
    : suspected ? '疑似异常 · 待技术确认'
      : domain?.primary_intent === 'configuration_guidance' ? '场景配置与使用指导' : '待补查或定位'
  const reply = [...new Set(item.claims.filter(claim => claim.role === 'staff').map(claim => claim.quote.trim()).filter(Boolean))]

  function copyReport() {
    copyText([
      `疑似异常 · ${title}`,
      ...facts.map(({ label, refs }) => `${label} ${refs.map(ref => `「${ref.quote}」`).join('；')}`),
      item.qa_id && `来源问答 #${item.qa_id}`,
      displayTime(item.asked_at) && `咨询时间 ${displayTime(item.asked_at)}`,
      '状态 待技术确认',
    ].filter(Boolean).join('\n'), '已复制疑似异常摘要。')
  }

  return <article className="space-y-4 rounded-xl border p-4 sm:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 space-y-1.5">
        <Badge variant="secondary" className="font-normal">{diagnosis}</Badge>
        <p className="text-xs text-muted-foreground">{[item.customer_name || '匿名商家', displayTime(item.asked_at)].filter(Boolean).join(' · ')}</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {item.qa_id && <Button variant="outline" size="sm" onClick={() => onOpenPair(normalizePair(item), questionAnchor(item))}>查看会话上下文<ArrowUpRight /></Button>}
        {suspected && <Button variant="outline" size="sm" onClick={copyReport}><Copy />复制疑似异常摘要</Button>}
      </div>
    </div>
    <p className="whitespace-pre-wrap text-sm font-medium leading-6 [overflow-wrap:anywhere]">{item.question_quote || item.question}</p>
    <dl className="grid gap-2 md:grid-cols-3">
      {facts.map(({ label, refs, missing }) => <div key={label} className="min-w-0 rounded-lg border bg-muted/25 p-3">
        <dt className="mb-2 text-xs font-medium">{label}</dt>
        <dd>{refs.length ? <FactQuotes item={item} refs={refs} onOpenPair={onOpenPair} /> : <p className="text-xs leading-5 text-muted-foreground">{missing}</p>}</dd>
      </div>)}
    </dl>
    {cause.length > 0 && <section className="space-y-2 border-t pt-3"><h5 className="text-sm font-medium">诊断依据</h5><FactQuotes item={item} refs={cause} onOpenPair={onOpenPair} /></section>}
    {verifiedSteps.length > 0 && <section className="space-y-2 border-t pt-3"><h5 className="text-sm font-medium">客服给出的处理步骤</h5><FactQuotes item={item} refs={verifiedSteps} onOpenPair={onOpenPair} /></section>}
    {verifiedSteps.length === 0 && reply.length > 0 && <section className="space-y-2 border-t pt-3">
      <h5 className="text-sm font-medium">{item.staff_source_verified ? '客服原话' : '回复原话 · 来源未核验'}</h5>
      {reply.map(quote => <p key={quote} className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{quote}</p>)}
    </section>}
  </article>
}

function TopicKnowledge({ items, onOpenPair }: { items: Knowledge[]; onOpenPair: OpenEvidencePair }) {
  const [showHistory, setShowHistory] = useState(false)
  const evaluated = items.filter(item => item.status === 'ready').map(item => ({ item, level: knowledgeReuse(item) }))
  const direct = evaluated.filter(entry => entry.level === 'direct')
  const history = evaluated.filter(entry => entry.level !== 'direct')
  return <section className="space-y-3" aria-label="处理办法">
    <h4 className="font-medium">处理办法</h4>
    {direct.length > 0 ? direct.map(({ item }) => <article key={item.id} className="space-y-3 rounded-xl border bg-muted/15 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2"><Badge>{knowledgeReuseLevel(item) === 'guidance' ? '可用指引' : '可直接复用'}</Badge><Button variant="outline" size="sm" onClick={() => copyKnowledge(item)}><Copy />{knowledgeReuseLevel(item) === 'guidance' ? '复制指引' : '复制处理办法'}</Button></div>
      <KnowledgeContent item={item} onOpenPair={onOpenPair} />
    </article>) : <p className="rounded-lg border bg-muted/20 px-4 py-3 text-sm text-muted-foreground">当前没有证据充分的可复用办法。</p>}
    {history.length > 0 && <>
      <Button variant="outline" size="sm" aria-expanded={showHistory} onClick={() => setShowHistory(value => !value)}>{showHistory ? '收起历史答复' : `查看历史答复与参考步骤（${history.length}）`}</Button>
      {showHistory && history.map(({ item, level }) => <article key={item.id} className="space-y-3 rounded-xl border p-4">
        <Badge variant="outline" className="font-normal">{reuseLabels[level]}</Badge>
        <KnowledgeContent item={item} onOpenPair={onOpenPair} />
      </article>)}
    </>}
  </section>
}

export function TopicWorkbench({ detail, onOpenPair, onAskTopic }: { detail: TopicDetail; onOpenPair: OpenEvidencePair; onAskTopic: () => void }) {
  const { topic, events, workbench } = detail
  const [caseId, setCaseId] = useState<string | null>(null)
  const [evidenceLimit, setEvidenceLimit] = useState(8)
  const caseHeading = useRef<HTMLHeadingElement>(null)
  const title = topic.theme.trim() || topic.question
  const trend = trendBadgeText(topic.trend)
  const representative = events.find(item => item.workbench?.suspected_report_ready)
    ?? events.find(item => item.staff_source_verified && confirmed.has(item.domain?.diagnosis.status ?? ''))
    ?? events.find(item => item.staff_source_verified)
    ?? events[0]
  const activeCase = events.find(item => item.id === caseId) ?? representative
  const otherCases = events.filter(item => item.id !== activeCase?.id)
  const gaps = workbench?.gaps.filter(gap => gap.count > 0) ?? []

  return <div className="qiyu-page-enter space-y-6">
    <div className="space-y-4 rounded-xl border bg-linear-to-br from-background to-muted/55 p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2"><h3 className="text-lg font-semibold tracking-tight [overflow-wrap:anywhere]">{title}</h3>{trend && <Badge variant="outline">{trend}</Badge>}</div>
          {topic.theme.trim() && topic.theme.trim() !== topic.question.trim() && <p className="text-sm text-muted-foreground [overflow-wrap:anywhere]">{topic.question}</p>}
        </div>
        <Button variant="outline" size="sm" onClick={onAskTopic}><MessagesSquare />追问这个问题</Button>
      </div>
      <dl className="grid grid-cols-3 gap-3 border-t pt-3 text-sm tabular-nums">
        <div><dt className="text-xs text-muted-foreground">咨询</dt><dd className="mt-1 font-semibold">{number(topic.consultation_count)} 次</dd></div>
        <div><dt className="text-xs text-muted-foreground">独立会话</dt><dd className="mt-1 font-semibold">{number(topic.session_count)} 个</dd></div>
        <div><dt className="text-xs text-muted-foreground">可识别商家</dt><dd className="mt-1 font-semibold">{number(topic.merchant_count)} 家</dd></div>
      </dl>
      <p className="text-xs leading-5 text-muted-foreground">{[topic.platforms?.join('、'), topic.last_seen && `最近发生 ${displayTime(topic.last_seen)}`, topic.unknown_merchant_sessions ? `${number(topic.unknown_merchant_sessions)} 个会话的商家身份待确认` : ''].filter(Boolean).join(' · ')}</p>
    </div>
    {workbench && <TrendComparison comparison={workbench.comparison} currentConsultations={topic.consultation_count} trend={topic.trend} />}
    {workbench && activeCase && <section className="space-y-3">
      <h4 ref={caseHeading} tabIndex={-1} className="scroll-mt-4 font-medium">处理判断 · {caseId ? '当前案例' : '代表案例'}</h4>
      <DecisionCase item={activeCase} title={title} onOpenPair={onOpenPair} />
      {gaps.length > 0 && <div className="space-y-2 rounded-lg border bg-muted/25 p-3">
        <h5 className="text-xs font-medium">待补证据</h5>
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">{gaps.map(gap => <li key={gap.code}>{gapLabels[gap.code] ?? '其他证据缺口'} {number(gap.count)} 次咨询</li>)}</ul>
      </div>}
    </section>}
    {workbench && <TopicKnowledge items={workbench.knowledge} onOpenPair={onOpenPair} />}
    {(!workbench || otherCases.length > 0) && <section className="space-y-3" aria-label="会话依据">
      <h4 className="font-medium">{workbench ? '其他会话依据' : '原句依据'}</h4>
      <EvidenceList evidence={(workbench ? otherCases : events).slice(0, evidenceLimit)} onOpenPair={onOpenPair}
        onSelectCase={workbench ? id => { setCaseId(id); caseHeading.current?.focus(); caseHeading.current?.scrollIntoView?.({ block: 'start' }) } : undefined} />
      {(workbench ? otherCases : events).length > evidenceLimit && <Button variant="outline" size="sm" onClick={() => setEvidenceLimit(value => value + 8)}>显示更多会话依据</Button>}
    </section>}
  </div>
}

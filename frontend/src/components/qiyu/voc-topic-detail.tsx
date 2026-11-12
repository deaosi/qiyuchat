import { useRef, useState, type ReactNode } from 'react'
import { Copy, MessagesSquare } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { DecisionCaseCard, DecisionCaseSection, TopicSummary } from './voc-detail-display'
import { Button } from '@/components/ui/button'
import { SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import type { Evidence, Knowledge, KnowledgeReuse, Ref, TopicDetail } from './voc-next-workspace'
import { diagnosisLabels, displayTime, EvidenceList, KnowledgeContent, knowledgeReuse, knowledgeReuseLevel, messageIdentifier, OriginalExchange, questionAnchor, type OpenTopicEvidence } from './voc-topic-evidence'

export const trendLabels = { all: '全部', new: '新出现', rising: '明显上升', persistent: '持续高频' } as const
export type TrendFilter = keyof typeof trendLabels
export type TopicTrend = { status: Exclude<TrendFilter, 'all'> | 'stable' | 'unknown'; previous_consultation_count: number | null; delta: number | null }
type Period = { start: string; end: string; source_pairs?: number; processed_pairs?: number }
export type TrendComparisonData = { current: Period; previous: Period | null; comparable: boolean; note: string; last_synced_at: string; episode?: { consultations: number; sessions: number } }

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
      {periods.map(({ label, period, consultations }) => {
        const coverage = period.processed_pairs !== undefined && period.source_pairs !== undefined
          ? `已整理 ${number(period.processed_pairs)} / ${number(period.source_pairs)} 条问答` : ''
        const summary = [consultations !== undefined && `${number(consultations)} 次咨询`, coverage].filter(Boolean).join(' · ')
        return <div key={label}>
          <dt className="font-medium">{label} <span className="ml-1 font-normal text-muted-foreground">{periodLabel(period)}</span></dt>
          <dd className="tabular-nums text-muted-foreground">{summary || '覆盖数据暂无记录'}</dd>
        </div>
      })}
      {comparison.episode && comparison.episode.consultations > 0 && <div>
        <dt className="font-medium">会话归并</dt>
        <dd className="tabular-nums text-muted-foreground">{number(comparison.episode.consultations)} 次咨询 · {number(comparison.episode.sessions)} 个会话</dd>
      </div>}
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

function FactQuotes({ item, refs, onOpenEvidence }: { item: Evidence; refs: Ref[]; onOpenEvidence: OpenTopicEvidence }) {
  return <ul className="space-y-2">{refs.map(ref => {
    const anchor = refAnchor(item, ref)
    const source = ref.role === 'unknown' ? '会话依据'
      : ref.source === 'question' ? '商家原话' : item.staff_source_verified ? '已核验客服原话' : '回复原话 · 来源未核验'
    return <li key={`${ref.source}-${ref.start}-${ref.end}`} className="min-w-0 space-y-1">
      <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{ref.quote}</p>
      <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
        <span>{source}</span>
        {item.qa_id && anchor && <Button type="button" variant="outline" size="xs" aria-label="跳转到原话" onClick={() => onOpenEvidence(item, anchor)}>跳转</Button>}
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

function DecisionCase({ item, title, onOpenEvidence, readOnly }: { item: Evidence; title: string; onOpenEvidence: OpenTopicEvidence; readOnly: boolean }) {
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

  function copyReport() {
    copyText([
      `疑似异常 · ${title}`,
      ...facts.map(({ label, refs }) => `${label} ${refs.map(ref => `「${ref.quote}」`).join('；')}`),
      item.qa_id && `来源问答 #${item.qa_id}`,
      displayTime(item.asked_at) && `咨询时间 ${displayTime(item.asked_at)}`,
      '状态 待技术确认',
    ].filter(Boolean).join('\n'), '已复制疑似异常摘要。')
  }

  return <DecisionCaseCard diagnosis={diagnosis} intro={<OriginalExchange item={item} />}
    metadata={[item.customer_name || '匿名商家', displayTime(item.asked_at)].filter(Boolean).join(' · ')}
    action={<>
        {item.qa_id && <Button type="button" variant="outline" size="xs" aria-label="跳转到会话上下文" onClick={() => onOpenEvidence(item, readOnly ? undefined : questionAnchor(item))}>跳转</Button>}
        {suspected && !readOnly && <Button variant="outline" size="sm" onClick={copyReport}><Copy />复制疑似异常摘要</Button>}
    </>}
    facts={facts.map(({ label, refs, missing }) => ({
      label,
      content: refs.length ? <FactQuotes item={item} refs={refs} onOpenEvidence={onOpenEvidence} /> : <p className="text-xs leading-5 text-muted-foreground">{missing}</p>,
    }))}>
    {cause.length > 0 && <DecisionCaseSection title="诊断依据"><FactQuotes item={item} refs={cause} onOpenEvidence={onOpenEvidence} /></DecisionCaseSection>}
    {verifiedSteps.length > 0 && <DecisionCaseSection title="客服给出的处理步骤"><FactQuotes item={item} refs={verifiedSteps} onOpenEvidence={onOpenEvidence} /></DecisionCaseSection>}
  </DecisionCaseCard>
}

function TopicKnowledge({ items, onOpenEvidence, readOnly }: { items: Knowledge[]; onOpenEvidence: OpenTopicEvidence; readOnly: boolean }) {
  const [showHistory, setShowHistory] = useState(false)
  const evaluated = items.filter(item => item.status === 'ready').map(item => ({ item, level: knowledgeReuse(item) }))
  const direct = evaluated.filter(entry => entry.level === 'direct')
  const history = evaluated.filter(entry => entry.level !== 'direct')
  return <section className="space-y-3" aria-label="处理办法">
    <h4 className="font-medium">处理办法</h4>
    {direct.length > 0 ? direct.map(({ item }) => <article key={item.id} className="space-y-3 rounded-xl border bg-muted/15 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2"><Badge>{knowledgeReuseLevel(item) === 'guidance' ? '可用指引' : '可直接复用'}</Badge>{!readOnly && <Button variant="outline" size="sm" onClick={() => copyKnowledge(item)}><Copy />{knowledgeReuseLevel(item) === 'guidance' ? '复制指引' : '复制处理办法'}</Button>}</div>
      <KnowledgeContent item={item} onOpenEvidence={onOpenEvidence} readOnly={readOnly} />
    </article>) : <p className="rounded-lg border bg-muted/20 px-4 py-3 text-sm text-muted-foreground">当前没有证据充分的可复用办法。</p>}
    {history.length > 0 && <>
      <Button variant="outline" size="sm" aria-expanded={showHistory} onClick={() => setShowHistory(value => !value)}>{showHistory ? '收起历史答复' : `查看历史答复与参考步骤（${history.length}）`}</Button>
      {showHistory && history.map(({ item, level }) => <article key={item.id} className="space-y-3 rounded-xl border p-4">
        <Badge variant="outline" className="font-normal">{reuseLabels[level]}</Badge>
        <KnowledgeContent item={item} onOpenEvidence={onOpenEvidence} readOnly={readOnly} />
      </article>)}
    </>}
  </section>
}

export function TopicDetailPanel({ standalone = false, exchangeOnly = false, children }: { standalone?: boolean; exchangeOnly?: boolean; children: ReactNode }) {
  const title = '问题与原句依据'
  const description = exchangeOnly ? '查看客户问题、客服回复及会话上下文。' : '查看问题的处理判断、办法和会话依据。'
  return <>
    <SheetHeader className="shrink-0 border-b px-4 sm:px-6">
      {standalone ? <>
        <h1 data-slot="sheet-title" className="font-semibold text-foreground">{title}</h1>
        <p className="sr-only">{description}</p>
      </> : <>
        <SheetTitle>{title}</SheetTitle><SheetDescription className="sr-only">{description}</SheetDescription>
      </>}
    </SheetHeader>
    <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">{children}</div>
  </>
}

function distinctQuestionCases(events: Evidence[]) {
  const cases = new Map<string, Evidence>()
  const quality = (item: Evidence) => {
    const verified = item.staff_source_verified && Boolean(item.answer.trim())
    const anchored = item.source_kind === 'source-anchored'
    return Number(verified) * 4 + Number(anchored) * 2 + Number(Boolean(item.question_quote?.trim()))
  }
  for (const item of events) {
    const id = typeof item.qa_id === 'number' || typeof item.qa_id === 'string' && /^\d+$/.test(item.qa_id) ? Number(item.qa_id) : NaN
    const key = Number.isSafeInteger(id) && id > 0 ? `qa:${id}` : `event:${item.id}`
    const previous = cases.get(key)
    if (!previous) { cases.set(key, item); continue }
    if (quality(item) > quality(previous)) cases.set(key, item)
  }
  return [...cases.values()]
}

export function TopicWorkbench({ detail, onOpenEvidence, onAskTopic, readOnly = false, exchangeOnly = false }: {
  detail: TopicDetail
  onOpenEvidence: OpenTopicEvidence
  onAskTopic?: () => void
  readOnly?: boolean
  exchangeOnly?: boolean
}) {
  const { topic, workbench } = detail
  const events = exchangeOnly ? distinctQuestionCases(detail.events) : detail.events
  const [caseId, setCaseId] = useState<string | null>(null)
  const [evidenceLimit, setEvidenceLimit] = useState(8)
  const caseHeading = useRef<HTMLHeadingElement>(null)
  const title = topic.theme.trim() || topic.question
  const trend = trendBadgeText(topic.trend)
  const representative = (exchangeOnly ? events.find(item => item.source_kind === 'source-anchored' && item.staff_source_verified && item.answer.trim())
    ?? events.find(item => item.staff_source_verified && item.answer.trim())
    ?? events.find(item => item.source_kind === 'source-anchored') : undefined)
    ?? events.find(item => item.workbench?.suspected_report_ready)
    ?? events.find(item => item.staff_source_verified && confirmed.has(item.domain?.diagnosis.status ?? ''))
    ?? events.find(item => item.staff_source_verified)
    ?? events[0]
  const selectedCase = exchangeOnly ? undefined : events.find(item => item.id === caseId)
  const activeCase = selectedCase ?? representative
  const otherCases = events.filter(item => item.id !== activeCase?.id)
  const gaps = workbench?.gaps.filter(gap => gap.count > 0) ?? []

  return <div className="qiyu-page-enter space-y-6">
    <TopicSummary title={title} badge={trend} description={topic.theme.trim() && topic.theme.trim() !== topic.question.trim() ? topic.question : undefined}
      action={!readOnly && onAskTopic && <Button variant="outline" size="sm" onClick={onAskTopic}><MessagesSquare />追问这个问题</Button>}
      metrics={[
        { label: '咨询', value: `${number(topic.consultation_count)} 次` },
        { label: '独立会话', value: `${number(topic.session_count)} 个` },
        { label: '可识别商家', value: `${number(topic.merchant_count)} 家` },
      ]}
      metadata={[topic.platforms?.join('、'), topic.last_seen && `最近发生 ${displayTime(topic.last_seen)}`, topic.unknown_merchant_sessions ? `${number(topic.unknown_merchant_sessions)} 个会话的商家身份待确认` : ''].filter(Boolean).join(' · ')} />
    {workbench && <TrendComparison comparison={workbench.comparison} currentConsultations={topic.consultation_count} trend={topic.trend} />}
    {workbench && activeCase && <section className="space-y-3">
      <h4 ref={caseHeading} tabIndex={-1} className="scroll-mt-4 font-medium">{exchangeOnly ? '代表问答' : `处理判断 · ${selectedCase ? '当前案例' : '代表案例'}`}</h4>
      {exchangeOnly ? <DecisionCaseCard metadata={[activeCase.customer_name || '匿名商家', displayTime(activeCase.asked_at)].filter(Boolean).join(' · ')}
        intro={<OriginalExchange item={activeCase} exactQuestion />} facts={[]}
        action={activeCase.qa_id && <Button type="button" variant="outline" size="xs" aria-label="跳转到会话上下文" onClick={() => onOpenEvidence(activeCase, questionAnchor(activeCase))}>跳转</Button>} />
        : <DecisionCase item={activeCase} title={title} onOpenEvidence={onOpenEvidence} readOnly={readOnly} />}
      {!exchangeOnly && gaps.length > 0 && <div className="space-y-2 rounded-lg border bg-muted/25 p-3">
        <h5 className="text-xs font-medium">待补证据</h5>
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">{gaps.map(gap => <li key={gap.code}>{gapLabels[gap.code] ?? '其他证据缺口'} {number(gap.count)} 次咨询</li>)}</ul>
      </div>}
    </section>}
    {workbench && !exchangeOnly && <TopicKnowledge items={workbench.knowledge} onOpenEvidence={onOpenEvidence} readOnly={readOnly} />}
    {(!workbench || otherCases.length > 0) && <section className="space-y-3" aria-label="会话依据">
      <h4 className="font-medium">{exchangeOnly ? (workbench ? '其他问答' : '问答记录') : workbench ? '其他会话依据' : '原句依据'}</h4>
      <EvidenceList evidence={(workbench ? otherCases : events).slice(0, evidenceLimit)} onOpenEvidence={onOpenEvidence} readOnly={readOnly} exchangeOnly={exchangeOnly}
        onSelectCase={workbench && !exchangeOnly ? id => { setCaseId(id); caseHeading.current?.focus(); caseHeading.current?.scrollIntoView?.({ block: 'start' }) } : undefined} />
      {(workbench ? otherCases : events).length > evidenceLimit && <Button variant="outline" size="sm" onClick={() => setEvidenceLimit(value => value + 8)}>显示更多会话依据</Button>}
    </section>}
  </div>
}

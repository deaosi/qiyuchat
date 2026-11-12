import { ArrowUpRight } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { VocKnowledgeKind, VocKnowledgeReuse } from '@/lib/api'
import type { DiagnosisStatus, Domain, Evidence, Knowledge, KnowledgeReuse, PrimaryIntent, Ref } from './voc-next-workspace'

export type OpenTopicEvidence = (item: Evidence, messageId?: string) => void

export function messageIdentifier(value: unknown) {
  if (typeof value === 'string') return value.trim() || undefined
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? String(value) : undefined
}

export function questionAnchor(item: Evidence) {
  if (item.workbench?.question_message_id) return item.workbench.question_message_id
  const ids = [...new Set((item.question_message_ids ?? []).map(messageIdentifier).filter(Boolean))]
  return ids.length === 1 ? ids[0] : undefined
}

export function knowledgeReuseLevel(item: Knowledge): VocKnowledgeReuse['level'] {
  if (item.knowledge_reuse) return item.knowledge_reuse.level
  if (item.reuse_level && item.reuse_level !== 'direct') return 'reference'
  const closure = item.conversation_verification
  return closure?.auto_reusable && closure.status === 'accepted' && closure.acceptance === 'resolved'
    && closure.effect === 'customer_reported_success' ? 'verified' : 'reference'
}

export function knowledgeReuse(item: Knowledge): KnowledgeReuse {
  const canonical = item.knowledge_reuse
  if (canonical && canonical.level !== 'reference') return 'direct'
  const closure = item.conversation_verification
  if (canonical?.reason_codes.some(code => /conflict|rejected|negative/.test(code))) return 'blocked'
  if (closure?.status === 'negative' || closure?.acceptance === 'continuing' || closure?.acceptance === 'rejected'
    || closure?.effect === 'customer_reported_failure' || closure?.reason_codes.some(code => code.includes('conflict'))) return 'blocked'
  if (item.reuse_level && item.reuse_level !== 'direct') return item.reuse_level
  if (canonical) return 'history'
  if (!closure?.auto_reusable || closure.status !== 'accepted') return 'history'
  if (knowledgeReuseLevel(item) === 'verified') return 'direct'
  return closure.acceptance === 'weak_closure' ? 'reference' : 'history'
}

export const intentLabels: Record<PrimaryIntent, string> = { configuration_guidance: '场景配置与使用指导', functional_anomaly: '功能异常与排障', unclear: '待识别背景' }
export const diagnosisLabels: Record<DiagnosisStatus, string> = { reported: '商家报告', pending: '待定位', config_cause_confirmed: '配置原因已确认', platform_limit_confirmed: '平台约束已确认', defect_confirmed: '缺陷已确认', not_applicable: '不适用' }
const knowledgeLabels: Record<VocKnowledgeKind, string> = { configuration_guide: '配置指引', troubleshooting_advice: '排查建议', verified_workaround: '绕行方案', capability_fact: '功能说明' }
const reuseLabels: Record<VocKnowledgeReuse['level'], string> = { verified: '操作效果已验证', guidance: '来源已核验', reference: '来源记录' }
export function displayTime(input?: string | number): string {
  if (!input) return ''
  const value = String(input)
  const parsed = Number(value)
  const date = /^\d+$/.test(value) ? new Date(parsed < 100_000_000_000 ? parsed * 1000 : parsed) : /^\d{4}-\d{2}-\d{2}T/.test(value) ? new Date(value) : null
  return !date || Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date).replaceAll('/', '-')
}

function SourceQuotes({ label, refs }: { label: string; refs: (Ref | null | undefined)[] }) {
  const present = refs.filter((ref): ref is Ref => Boolean(ref?.quote))
  if (!present.length) return null
  return <div className="space-y-1"><div className="text-xs font-medium text-foreground">{label}</div>{present.map((ref, index) => <p key={index} className="whitespace-pre-wrap break-words text-sm text-muted-foreground">“{ref.quote}” <span className="text-xs">· {ref.role === 'unknown' ? '会话依据' : ref.source === 'question' ? '商家原话' : '回复原话'}</span></p>)}</div>
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

export function OriginalExchange({ item }: { item: Evidence }) {
  const claims = [...new Set(item.claims.filter(claim => claim.role === 'staff').map(claim => claim.quote.trim()).filter(Boolean))]
  const reply = item.staff_source_verified ? item.answer.trim() || claims.join('\n') : ''
  const missing = item.answer.trim() || claims.length
    ? '回复来源尚未核验，请查看会话上下文确认。'
    : '当前问答片段未提取到客服回复，请查看会话上下文。'
  return <div className="space-y-3 rounded-lg border bg-muted/20 p-4 text-sm">
    <div className="space-y-1"><h5 className="font-medium">客户问题原文</h5>
      <p className="whitespace-pre-wrap leading-6 [overflow-wrap:anywhere]">{item.question || item.question_quote || '未记录咨询原文'}</p></div>
    <div className="space-y-1"><h5 className="font-medium">客服回复原文</h5>
      {reply && <p className="text-xs text-muted-foreground">同一问答中的原始回复，可能包含其他咨询内容。</p>}
      <p className={`whitespace-pre-wrap leading-6 [overflow-wrap:anywhere]${reply ? '' : ' text-muted-foreground'}`}>{reply || missing}</p></div>
  </div>
}

export function EvidenceList({ evidence, onOpenEvidence, onSelectCase, readOnly = false, exchangeOnly = false }: {
  evidence: Evidence[]
  onOpenEvidence: OpenTopicEvidence
  onSelectCase?: (id: string) => void
  readOnly?: boolean
  exchangeOnly?: boolean
}) {
  if (!evidence.length) return <p className="text-sm text-muted-foreground">暂无可展示的原句依据。</p>
  return <div className="space-y-3">{evidence.map(item => {
    return <div key={item.id} className="rounded-lg border bg-muted/20 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>{!readOnly && item.qa_id ? `问答 ${item.qa_id} · ` : ''}{item.customer_name || '匿名商家'}{displayTime(item.asked_at) ? ` · ${displayTime(item.asked_at)}` : ''}</span>
        <div className="flex flex-wrap gap-1">
          {onSelectCase && <Button variant="outline" size="xs" onClick={() => onSelectCase(item.id)}>{exchangeOnly ? '查看问答' : '查看处理判断'}</Button>}
          {item.qa_id && <Button variant="ghost" size="xs" onClick={() => onOpenEvidence(item, readOnly ? undefined : questionAnchor(item))}>查看会话上下文 {!exchangeOnly && <ArrowUpRight />}</Button>}
        </div>
      </div>
      <div className="mt-2"><OriginalExchange item={item} /></div>
      {!exchangeOnly && <DomainEvidence domain={item.domain} />}
    </div>
  })}</div>
}

export function KnowledgeContent({ item, onOpenEvidence, readOnly = false }: {
  item: Knowledge
  onOpenEvidence: OpenTopicEvidence
  readOnly?: boolean
}) {
  const kind = item.kind ? knowledgeLabels[item.kind] : '类型待识别'
  const confirmed = item.diagnosis_status && ['config_cause_confirmed', 'platform_limit_confirmed', 'defect_confirmed'].includes(item.diagnosis_status)
  const closure = item.conversation_verification
  const level = knowledgeReuseLevel(item)
  const feedbackEvidence = item.knowledge_reuse?.evidence ?? closure?.evidence
  const guidance = item.kind === 'configuration_guide'
    ? '客服原话和适用条件已核验，可按所列条件使用。'
    : '客服原话和适用条件已核验，可用于排查。操作效果待验证。'
  const reuseDescription = level === 'verified' ? '客户明确反馈操作成功，可按所列条件复用。'
    : level === 'guidance' ? guidance : '保留此记录供查阅原始会话。'
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center gap-2"><Badge variant="secondary" className="font-normal">{kind}</Badge><Badge variant={level === 'verified' ? 'default' : 'outline'} className="font-normal">{reuseLabels[level]}</Badge>{item.diagnosis_status && item.diagnosis_status !== 'not_applicable' && <Badge variant="outline" className="font-normal">{diagnosisLabels[item.diagnosis_status] || '待核对'}</Badge>}</div>
    <div className="rounded-lg border bg-muted/20 p-3 text-sm">
      <p>{reuseDescription}</p>
      {closure?.acceptance === 'understood' && <p className="mt-1 text-muted-foreground">客户已接受解释。</p>}
      {feedbackEvidence?.map((ref, index) => {
        const anchor = messageIdentifier(ref.message_id)
        const qaId = 'qa_id' in ref ? messageIdentifier(ref.qa_id) : undefined
        const source = qaId ? item.evidence.find(evidence => String(evidence.qa_id) === qaId) : undefined
        return <p key={index} className="mt-2 whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">
          “{ref.quote}” · {ref.role === 'user' || ref.role === 'merchant' ? '客户反馈' : '会话证据'}{ref.ts ? ` · ${displayTime(ref.ts)}` : ''}{!readOnly && anchor ? ` · 消息 ${anchor}` : ''}
          {source && anchor && <Button variant="ghost" size="xs" onClick={() => onOpenEvidence(source, anchor)}>定位原话<ArrowUpRight /></Button>}
        </p>
      })}
    </div>
    <h3 className="font-medium">{item.question}</h3><p className="whitespace-pre-wrap break-words text-sm">{item.answer}</p>
    {item.steps.length > 0 && <div className="text-sm"><span className="font-medium">步骤</span><ol className="mt-1 list-inside list-decimal space-y-1">{item.steps.map((step, i) => <li key={i}>{step}</li>)}</ol></div>}
    {item.scope && <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">适用范围：{item.scope}</p>}
    <SourceQuotes label="适用条件依据" refs={item.applicability || []} />
    {confirmed && <SourceQuotes label="已确认原因依据" refs={[item.confirmed_cause]} />}
    {item.reply_patterns.length > 0 && <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">日常话术：{item.reply_patterns.join('；')}</p>}
    {item.cautions.length > 0 && <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">注意事项：{item.cautions.join('；')}</p>}
    <div><h4 className="mb-2 text-sm font-medium">来源依据</h4><EvidenceList evidence={item.evidence} onOpenEvidence={onOpenEvidence} readOnly={readOnly} /></div>
  </div>
}

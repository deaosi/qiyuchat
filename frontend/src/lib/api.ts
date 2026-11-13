import type { QAPair } from '@/lib/demo-data'
import { withRequestActivity } from '@/lib/request-activity'

export interface ApiFilters {
  source: 'local'
  days?: number
  start?: string
  end?: string
  group?: string
  staff?: string
  kind?: FeishuKind
  cluster?: string | number
  customer?: string
  q?: string
  entity_q?: string
  entity_sort?: 'questions' | 'unanswered' | 'recent'
  cluster_members?: 'repeated' | 'single'
  replied?: string
  limit?: number
  offset?: number
  first_page?: 1
  id?: string | number
  top?: number
  status?: VocStatus | KnowledgeStatus | 'all'
  topic_q?: string
  evidence_status?: 'all' | 'pending' | 'confirmed'
  topic_id?: number
  focus_topic?: string
  workbench?: 1
  trend?: 'all' | 'new' | 'rising' | 'persistent'
  support_intent?: 'all' | 'configuration_guidance' | 'functional_anomaly' | 'unclear'
  diagnosis_status?: 'all' | 'reported' | 'pending' | 'config_cause_confirmed' | 'platform_limit_confirmed' | 'defect_confirmed' | 'not_applicable'
}

export type FeishuKind = 'topics' | 'sops'
export type FeishuPreview = {
  title: string
  count: number
  total: number
  items: { title: string; summary: string }[]
  start: string
  end: string
  fingerprint: string
  publish_configured: boolean
  destination?: string
  content?: string
}
export type FeishuPublication = { url: string; title: string; reused: boolean; count?: number }

export type VocQueryFilters = {
  [K in 'days' | 'start' | 'end' | 'group' | 'staff' | 'customer' | 'q' | 'support_intent' | 'diagnosis_status' | 'replied' | 'cluster' | 'focus_topic']?: ApiFilters[K] | null
}

const detailRepliesStatuses = ['found', 'no_reply', 'unmatched', 'unavailable', 'busy'] as const
const detailSenderKinds = ['fuke_staff', 'group_participant', 'unidentified'] as const
export type DetailSenderKind = typeof detailSenderKinds[number]
export interface DetailSenderMetadata {
  sender_name?: string
  sender_kind?: DetailSenderKind
  sender_account?: string
}
export type DetailMessageImage = { url: string }
export interface DetailRepliesResponse {
  status: typeof detailRepliesStatuses[number]
  context_revision?: string
  senders?: (DetailSenderMetadata & { context_seq: number })[]
  messages: {
    role: 'staff'
    sender_name?: string
    content: string
    ts: number
    time: string
    source_message_id: string
    is_question: false
    is_answer: false
    images?: DetailMessageImage[]
  }[]
}

export class ApiError extends Error {
  status: number
  constructor(message: string, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function apiUrl(name: string, filters: ApiFilters): string {
  const params = new URLSearchParams()
  Object.entries({ ...filters, source: 'local' }).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })
  return `/api/${name}?${params}`
}

async function readResponse<T>(response: Response, redirectOnUnauthorized = true): Promise<T> {
  if (response.status === 401) {
    if (redirectOnUnauthorized && window.location.pathname !== '/login') window.location.assign('/login')
    throw new ApiError('登录已过期，请重新登录。', 401)
  }
  let data: unknown
  try {
    data = await response.json()
  } catch {
    throw new ApiError('服务返回了无法读取的数据，请稍后重试。', response.status)
  }
  if (!response.ok) {
    const error = data && typeof data === 'object' && 'error' in data ? String(data.error) : '请求失败，请稍后重试。'
    throw new ApiError(error, response.status)
  }
  return data as T
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function validSenderMetadata(message: Record<string, unknown>): boolean {
  return (message.sender_name === undefined || typeof message.sender_name === 'string')
    && (message.sender_account === undefined || typeof message.sender_account === 'string')
    && (message.sender_kind === undefined || detailSenderKinds.some(kind => kind === message.sender_kind))
}

function validContextSequence(value: unknown): value is number {
  return isCount(value) && Number.isSafeInteger(value)
}

function validContextRevision(value: unknown): boolean {
  return value === undefined || typeof value === 'string' && Boolean(value.trim())
}

export function httpsLink(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value) return undefined
  try {
    const url = new URL(value)
    if (url.protocol === 'https:' && !url.username && !url.password) return url.href
  } catch { /* A destination may be a label instead of a URL. */ }
  return undefined
}

function hasCounts(value: Record<string, unknown>, keys: string[]): boolean {
  return keys.every(key => isCount(value[key]))
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === 'string')
}

function validDetailImages(value: unknown): boolean {
  if (value === undefined) return true
  return Array.isArray(value) && value.every(image => {
    if (!isRecord(image) || typeof image.url !== 'string') return false
    try {
      const url = new URL(image.url)
      return url.origin === 'https://ysf.nosdn.127.net' && !url.username && !url.password
    } catch {
      return false
    }
  })
}

const vocKnowledgeKinds = ['configuration_guide', 'troubleshooting_advice', 'verified_workaround', 'capability_fact'] as const
export type VocKnowledgeKind = typeof vocKnowledgeKinds[number]
const vocKnowledgeReuseLevels = ['verified', 'guidance', 'reference'] as const
type VocVerificationEvidence = { quote: string; role: string; message_id?: string | number; ts?: string | number }
export type VocKnowledgeReuse = { level: typeof vocKnowledgeReuseLevels[number]; reason_codes: string[]; evidence?: VocVerificationEvidence[] }
export type VocConversationVerification = {
  status: 'accepted' | 'weak' | 'negative' | 'unknown'
  acceptance: 'resolved' | 'understood' | 'weak_closure' | 'continuing' | 'rejected' | 'unknown'
  effect: 'customer_reported_success' | 'customer_reported_failure' | 'not_established' | 'unknown'
  auto_reusable: boolean
  evidence: VocVerificationEvidence[]
  coverage: Record<string, unknown>
  reason_codes: string[]
  version: string
  fingerprint: string
}

const vocSopKinds = ['configuration', 'anomaly'] as const
const vocSopStatuses = ['verified', 'guidance', 'incomplete'] as const
const vocSopPhases = ['diagnosis', 'action', 'retest', 'escalation'] as const
const vocSopOutcomes = ['verified', 'failed', 'unconfirmed'] as const
export type VocSopEvidence = { message_id: string; quote: string; ts: number }
export type VocSopStep = {
  phase: typeof vocSopPhases[number]
  evidence: VocSopEvidence
  outcome: typeof vocSopOutcomes[number]
  feedback?: VocSopEvidence
}
export type VocSop = {
  id: string
  topic_id: string
  qa_id: number
  title: string
  kind: typeof vocSopKinds[number]
  status: typeof vocSopStatuses[number]
  problem: VocSopEvidence
  conditions: VocSopEvidence[]
  replies?: VocSopEvidence[]
  steps: VocSopStep[]
  result: VocSopEvidence | null
  missing: string[]
}

const vocPilotFamilies = ['no_reply', 'scenario_config', 'configuration', 'anomaly', 'platform_limit', 'other'] as const
const vocPilotEvidenceStatuses = ['reported', 'staff_investigated', 'cause_confirmed'] as const
const vocPlaybookStatuses = ['investigation', 'guidance', 'verified'] as const
const vocCasePathStepStates = ['proposed', 'reported', 'instructed'] as const
const vocCaseSolutionKinds = ['problem', 'diagnosis', 'action', 'decision', 'verification', 'next_step'] as const
export type VocPilotFamily = typeof vocPilotFamilies[number]
export type VocPilotEvidence = { qa_id: number; message_id: string; quote: string; ts: number }
export type VocPilotCase = VocPilotEvidence & {
  anchored: boolean
  has_reply: boolean
  details?: { goal?: string; observed?: string; checked?: string }
}
export type VocPilotIssue = {
  id: string
  family: VocPilotFamily
  title: string
  platform: string
  module: string
  condition: string
  scope_status?: 'needs_context' | 'scoped'
  evidence_status: typeof vocPilotEvidenceStatuses[number]
  impact: { consultations: number; sessions: number; merchants: number; unidentified_sessions: number }
  next: { owner: string; action: string; missing: string[] }
  cases: VocPilotCase[]
}
export type VocIssuePilot = {
  items: VocPilotIssue[]
  total: number
  first_page_total?: number
  coverage: { source_events: number; matched_consultations: number; classified_consultations: number; needs_context: number }
}
export type VocPlaybookBranch = {
  id?: string
  condition: string
  steps: { text: string; evidence: VocPilotEvidence[] }[]
  retest: string
  escalation: string
  status?: 'guidance' | 'verified'
  case_count?: number
  retest_evidence?: VocPilotEvidence[]
  escalation_evidence?: VocPilotEvidence[]
  condition_evidence?: VocPilotEvidence[]
  result_evidence?: VocPilotEvidence[]
}
export type VocPlaybook = Pick<VocPilotIssue, 'id' | 'family' | 'title' | 'platform' | 'module' | 'condition'> & {
  issue_id: string
  status: typeof vocPlaybookStatuses[number]
  checked_cases: number
  triage: { check: string; when_yes: string; when_no: string }[]
  branches: VocPlaybookBranch[]
  missing: string[]
  evidence: VocPilotEvidence[]
  cases?: VocPilotCase[]
  impact?: VocPilotIssue['impact']
  counterevidence?: VocPilotEvidence[]
}
export type VocCaseSolution = {
  title: string
  sections: { kind: typeof vocCaseSolutionKinds[number]; text: string; evidence: VocPilotEvidence[]; mode?: 'expected' | 'observed' }[]
}
export type VocCasePath = {
  id: string
  qa_id: number
  problem: VocPilotEvidence
  steps: { actor: 'staff' | 'merchant'; state: typeof vocCasePathStepStates[number]; action: string; evidence: VocPilotEvidence }[]
  platform: string
  criterion?: VocPilotEvidence
  solution?: VocCaseSolution
  outcome: 'not_recorded'
}
export type VocPlaybookResultsData = { items: VocPlaybook[]; total: number; case_paths?: VocCasePath[]; first_page_total?: number }

function validateVoc2Response(name: string, data: unknown, workbench = false): void {
  if (!name.startsWith('voc2/')) return
  const messageRefs = (v: unknown) => Array.isArray(v) && v.every(item => isRecord(item)
    && (typeof item.message_id === 'string' || Number.isSafeInteger(item.message_id))
    && isCount(item.start) && isCount(item.end) && item.start < item.end)
  const ref = (v: unknown) => isRecord(v) && ['question', 'answer'].includes(String(v.source))
    && typeof v.quote === 'string' && isCount(v.start) && isCount(v.end) && typeof v.role === 'string' && v.alignment === 'exact'
    && (v.source_refs === undefined || messageRefs(v.source_refs))
  const nullableRef = (v: unknown) => v === null || v === undefined || ref(v)
  const refs = (v: unknown) => Array.isArray(v) && v.every(ref)
  const verificationEvidence = (v: unknown) => Array.isArray(v) && v.every(item => isRecord(item)
    && typeof item.quote === 'string' && typeof item.role === 'string'
    && (item.message_id === undefined || typeof item.message_id === 'string' || typeof item.message_id === 'number')
    && (item.ts === undefined || typeof item.ts === 'string' || typeof item.ts === 'number'))
  const closure = (v: unknown) => isRecord(v) && ['accepted', 'weak', 'negative', 'unknown'].includes(String(v.status))
    && ['resolved', 'understood', 'weak_closure', 'continuing', 'rejected', 'unknown'].includes(String(v.acceptance))
    && ['customer_reported_success', 'customer_reported_failure', 'not_established', 'unknown'].includes(String(v.effect))
    && typeof v.auto_reusable === 'boolean' && (!v.auto_reusable || v.status === 'accepted')
    && isRecord(v.coverage) && isStringArray(v.reason_codes)
    && typeof v.version === 'string' && typeof v.fingerprint === 'string'
    && verificationEvidence(v.evidence)
  const intents = ['configuration_guidance', 'functional_anomaly', 'unclear']
  const diagnoses = ['reported', 'pending', 'config_cause_confirmed', 'platform_limit_confirmed', 'defect_confirmed', 'not_applicable']
  const domain = (v: unknown) => isRecord(v) && intents.includes(String(v.primary_intent))
    && ['request', 'follow_up', 'reproduction_context'].includes(String(v.fragment_role))
    && ['platforms', 'modules', 'reproduction_context', 'prerequisites', 'operation_path'].every(key => refs(v[key]))
    && nullableRef(v.merchant_goal) && nullableRef(v.expected) && nullableRef(v.actual)
    && isRecord(v.diagnosis) && diagnoses.includes(String(v.diagnosis.status))
    && typeof v.diagnosis.cause_code === 'string' && nullableRef(v.diagnosis.cause) && refs(v.diagnosis.evidence)
    && Array.isArray(v.actions) && v.actions.every(action => isRecord(action) && typeof action.method_code === 'string'
      && ref(action.instruction) && ['suggested', 'verified'].includes(String(action.effect_status)) && nullableRef(action.verification))
    && isStringArray(v.tags)
  const categoryKey = (value: unknown) => typeof value === 'string' && /^t_[a-f0-9]{24}$/.test(value)
  const topicMembers = (members: unknown, representativeId: string, minimum: number) => Array.isArray(members) && members.length >= minimum
    && members.every(member => isRecord(member) && typeof member.id === 'string' && Boolean(member.id)
      && typeof member.question === 'string' && Boolean(member.question.trim()) && isCount(member.consultation_count)
      && (member.theme === undefined || typeof member.theme === 'string'))
    && members[0].id === representativeId && new Set(members.map(member => member.id)).size === members.length
  const topic = (v: unknown) => isRecord(v) && typeof v.id === 'string' && typeof v.question === 'string'
    && typeof v.theme === 'string' && typeof v.last_seen === 'string'
    && hasCounts(v, ['event_count', 'customer_count', 'session_count', 'mention_count']) && isStringArray(v.samples)
    && (v.primary_intent === undefined || intents.includes(String(v.primary_intent)))
    && (v.consultation_count === undefined || isCount(v.consultation_count))
    && (v.category_key === undefined
      ? v.members === undefined || topicMembers(v.members, v.id, 2)
      : categoryKey(v.category_key) && topicMembers(v.members, v.id, 1))
  const evidence = (v: unknown) => isRecord(v) && typeof v.id === 'string' && typeof v.event_id === 'string'
    && typeof v.question === 'string' && typeof v.answer === 'string' && typeof v.staff_source_verified === 'boolean'
    && Array.isArray(v.claims) && v.claims.every(claim => isRecord(claim) && typeof claim.role === 'string'
      && typeof claim.quote === 'string' && isCount(claim.start) && isCount(claim.end) && Array.isArray(claim.source_refs))
    && (v.domain === undefined || domain(v.domain))
  const reuse = (v: unknown) => isRecord(v) && vocKnowledgeReuseLevels.some(level => level === v.level)
    && isStringArray(v.reason_codes) && (v.evidence === undefined || verificationEvidence(v.evidence))
  const knowledge = (v: unknown) => isRecord(v) && typeof v.id === 'string' && typeof v.topic_id === 'string'
    && typeof v.question === 'string' && typeof v.answer === 'string' && typeof v.scope === 'string'
    && isStringArray(v.steps) && isStringArray(v.reply_patterns) && isStringArray(v.cautions)
    && Array.isArray(v.evidence) && v.evidence.every(evidence)
    && (v.kind === undefined || (typeof v.kind === 'string' && vocKnowledgeKinds.some(kind => kind === v.kind)))
    && (v.diagnosis_status === undefined || diagnoses.includes(String(v.diagnosis_status)))
    && (v.applicability === undefined || refs(v.applicability)) && nullableRef(v.confirmed_cause)
    && (v.reuse_level === undefined || ['direct', 'reference', 'history', 'blocked'].includes(String(v.reuse_level)))
    && (v.conversation_verification === undefined || closure(v.conversation_verification))
    && (v.knowledge_reuse === undefined || reuse(v.knowledge_reuse))
  const sopEvidence = (v: unknown) => isRecord(v) && typeof v.message_id === 'string' && Boolean(v.message_id.trim())
    && typeof v.quote === 'string' && Boolean(v.quote.trim()) && isCount(v.ts)
  const sop = (v: unknown) => isRecord(v) && typeof v.id === 'string' && Boolean(v.id.trim()) && typeof v.topic_id === 'string'
    && isCount(v.qa_id) && v.qa_id > 0 && typeof v.title === 'string' && Boolean(v.title.trim())
    && vocSopKinds.some(kind => kind === v.kind) && vocSopStatuses.some(status => status === v.status)
    && sopEvidence(v.problem) && Array.isArray(v.conditions) && v.conditions.every(sopEvidence)
    && (v.replies === undefined || Array.isArray(v.replies) && v.replies.every(sopEvidence))
    && Array.isArray(v.steps) && v.steps.every(step => isRecord(step) && vocSopPhases.some(phase => phase === step.phase)
      && vocSopOutcomes.some(outcome => outcome === step.outcome) && sopEvidence(step.evidence)
      && (step.feedback === undefined || sopEvidence(step.feedback)))
    && (v.result === null || sopEvidence(v.result)) && isStringArray(v.missing)
  const pilotBase = (v: Record<string, unknown>) => typeof v.id === 'string' && Boolean(v.id.trim())
    && vocPilotFamilies.some(family => family === v.family)
    && typeof v.title === 'string' && Boolean(v.title.trim())
    && ['platform', 'module', 'condition'].every(key => typeof v[key] === 'string')
  const pilotEvidence = (v: unknown, needsAnchor = true) => isRecord(v) && isCount(v.qa_id) && v.qa_id > 0
    && typeof v.message_id === 'string' && (!needsAnchor || Boolean(v.message_id.trim()))
    && typeof v.quote === 'string' && Boolean(v.quote.trim()) && isCount(v.ts)
  const pilotEvidenceList = (v: unknown) => Array.isArray(v) && v.every(item => pilotEvidence(item))
  const caseSolution = (v: unknown) => isRecord(v) && typeof v.title === 'string' && Boolean(v.title.trim())
    && Array.isArray(v.sections) && v.sections.length > 0 && v.sections.every(section => isRecord(section)
      && vocCaseSolutionKinds.some(kind => kind === section.kind)
      && (section.mode === undefined || section.kind === 'verification' && (section.mode === 'expected' || section.mode === 'observed'))
      && typeof section.text === 'string' && Boolean(section.text.trim())
      && Array.isArray(section.evidence) && section.evidence.length > 0 && pilotEvidenceList(section.evidence))
  const casePath = (v: unknown) => isRecord(v) && typeof v.id === 'string' && Boolean(v.id.trim())
    && typeof v.platform === 'string' && v.outcome === 'not_recorded'
    && isRecord(v.problem) && v.problem.qa_id === v.qa_id && pilotEvidence(v.problem)
    && (v.criterion === undefined || pilotEvidence(v.criterion))
    && (v.solution === undefined || caseSolution(v.solution))
    && Array.isArray(v.steps) && (v.steps.length > 0 || v.solution !== undefined) && v.steps.every(step => isRecord(step)
      && (step.actor === 'staff' || step.actor === 'merchant')
      && vocCasePathStepStates.some(state => state === step.state)
      && typeof step.action === 'string' && Boolean(step.action.trim())
      && isRecord(step.evidence) && typeof step.evidence.quote === 'string'
      && step.evidence.quote.includes(step.action) && pilotEvidence(step.evidence))
  const pilotImpact = (v: unknown) => isRecord(v) && hasCounts(v, ['consultations', 'sessions', 'merchants', 'unidentified_sessions'])
  const pilotCaseDetails = (v: unknown) => isRecord(v)
    && [v.goal, v.observed, v.checked].every(value => value === undefined || typeof value === 'string')
  const pilotCases = (v: unknown) => Array.isArray(v) && v.every(item => isRecord(item)
    && typeof item.anchored === 'boolean' && typeof item.has_reply === 'boolean' && pilotEvidence(item, item.anchored)
    && (item.details === undefined || pilotCaseDetails(item.details)))
  const pilotIssue = (v: unknown) => isRecord(v) && pilotBase(v)
    && vocPilotEvidenceStatuses.some(status => status === v.evidence_status)
    && (v.scope_status === undefined || v.scope_status === 'needs_context' || v.scope_status === 'scoped')
    && pilotImpact(v.impact)
    && isRecord(v.next) && typeof v.next.owner === 'string' && typeof v.next.action === 'string' && isStringArray(v.next.missing)
    && pilotCases(v.cases)
  const playbook = (v: unknown) => isRecord(v) && pilotBase(v) && typeof v.issue_id === 'string'
    && vocPlaybookStatuses.some(status => status === v.status) && isCount(v.checked_cases)
    && Array.isArray(v.triage) && v.triage.every(item => isRecord(item)
      && ['check', 'when_yes', 'when_no'].every(key => typeof item[key] === 'string'))
    && Array.isArray(v.branches) && v.branches.every(branch => isRecord(branch)
      && ['condition', 'retest', 'escalation'].every(key => typeof branch[key] === 'string')
      && (branch.id === undefined || typeof branch.id === 'string')
      && (branch.status === undefined || branch.status === 'guidance' || branch.status === 'verified')
      && (branch.case_count === undefined || isCount(branch.case_count))
      && (branch.retest_evidence === undefined || pilotEvidenceList(branch.retest_evidence))
      && (branch.escalation_evidence === undefined || pilotEvidenceList(branch.escalation_evidence))
      && (branch.condition_evidence === undefined || pilotEvidenceList(branch.condition_evidence))
      && (branch.result_evidence === undefined || pilotEvidenceList(branch.result_evidence))
      && Array.isArray(branch.steps) && branch.steps.every(step => isRecord(step) && typeof step.text === 'string'
        && pilotEvidenceList(step.evidence)))
    && isStringArray(v.missing) && pilotEvidenceList(v.evidence)
    && (v.cases === undefined || pilotCases(v.cases)) && (v.impact === undefined || pilotImpact(v.impact))
    && (v.counterevidence === undefined || pilotEvidenceList(v.counterevidence))
  const job = (v: unknown) => isRecord(v) && typeof v.id === 'string'
    && ['queued', 'running', 'completed', 'partial', 'failed', 'skipped'].includes(String(v.status))
    && hasCounts(v, ['total', 'processed', 'failed', 'skipped', 'usage_tokens'])
    && typeof v.error === 'string'
  const period = (v: unknown) => isRecord(v) && typeof v.start === 'string' && typeof v.end === 'string'
    && hasCounts(v, ['source_pairs', 'processed_pairs'])
  const comparison = (v: unknown) => isRecord(v) && period(v.current) && (v.previous === null || period(v.previous))
    && typeof v.comparable === 'boolean' && typeof v.note === 'string' && typeof v.last_synced_at === 'string'
    && (v.episode === undefined || isRecord(v.episode) && hasCounts(v.episode, ['consultations', 'sessions']))
  const enrichedTopic = (v: unknown) => isRecord(v) && topic(v) && isStringArray(v.platforms)
    && hasCounts(v, ['merchant_count', 'unknown_merchant_sessions']) && isRecord(v.trend)
    && ['new', 'rising', 'persistent', 'stable', 'unknown'].includes(String(v.trend.status))
    && (v.trend.previous_consultation_count === null || isCount(v.trend.previous_consultation_count))
    && (v.trend.delta === null || (typeof v.trend.delta === 'number' && Number.isFinite(v.trend.delta)))
  const enrichedEvidence = (v: unknown) => isRecord(v) && evidence(v) && (v.workbench === undefined
    || isRecord(v.workbench) && refs(v.workbench.commerce_settings) && refs(v.workbench.scenario_conditions)
    && refs(v.workbench.actual) && (v.workbench.question_message_id === undefined || typeof v.workbench.question_message_id === 'string')
    && (v.workbench.suspected_report_ready === undefined || typeof v.workbench.suspected_report_ready === 'boolean'))
  let valid = isRecord(data)
  if (valid && isRecord(data)) switch (name) {
    case 'voc2/summary': valid = hasCounts(data, ['source_pairs', 'total_events', 'answered_events', 'topic_count', 'ready_knowledge', 'exception_count'])
      && (data.auto_reusable_knowledge === undefined || isCount(data.auto_reusable_knowledge))
      && isRecord(data.semantics) && typeof data.semantics.ready === 'boolean' && typeof data.semantics.model === 'string'
      && isRecord(data.coverage) && hasCounts(data.coverage, ['processed_pairs', 'pending_pairs', 'answered_pairs'])
      && (data.latest_job === null || job(data.latest_job)); break
    case 'voc2/topics': valid = isCount(data.total) && Array.isArray(data.items) && data.items.every(workbench ? enrichedTopic : topic)
      && (data.category_total === undefined || isCount(data.category_total) && Number(data.category_total) >= Number(data.total))
      && (data.category_consultation_count === undefined || isCount(data.category_consultation_count))
      && (!workbench || comparison(data.comparison)); break
    case 'voc2/knowledge': valid = data.item !== undefined ? knowledge(data.item) : isCount(data.total) && Array.isArray(data.items) && data.items.every(knowledge); break
    case 'voc2/sops': valid = isCount(data.total) && Array.isArray(data.items) && data.items.every(sop); break
    case 'voc2/issue-pilot': valid = isCount(data.total) && Array.isArray(data.items) && data.items.every(pilotIssue)
      && isRecord(data.coverage) && hasCounts(data.coverage, ['source_events', 'matched_consultations', 'classified_consultations', 'needs_context']); break
    case 'voc2/playbooks': valid = isCount(data.total) && Array.isArray(data.items) && data.items.every(playbook)
      && (data.case_paths === undefined || Array.isArray(data.case_paths) && data.case_paths.every(casePath)); break
    case 'voc2/complete-sops': valid = isCount(data.total) && data.case_paths === undefined && Array.isArray(data.items)
      && data.items.every(item => playbook(item) && item.status === 'verified' && Array.isArray(item.branches)
        && item.branches.length > 0 && item.branches.every((branch: unknown) => isRecord(branch) && branch.status === 'verified')); break
    case 'voc2/exceptions': valid = isCount(data.total) && Array.isArray(data.items) && data.items.every(item => isRecord(item)
      && ['id', 'kind', 'question', 'reason', 'created_at'].every(key => typeof item[key] === 'string')); break
    case 'voc2/topic': valid = (workbench ? enrichedTopic(data.topic) : topic(data.topic))
      && Array.isArray(data.events) && data.events.every(workbench ? enrichedEvidence : evidence)
      && (!workbench || isRecord(data.workbench) && comparison(data.workbench.comparison)
        && Array.isArray(data.workbench.knowledge) && data.workbench.knowledge.every(knowledge)
        && Array.isArray(data.workbench.gaps) && data.workbench.gaps.every(gap => isRecord(gap) && typeof gap.code === 'string' && isCount(gap.count))); break
    case 'voc2/event': valid = evidence(data.event) && (data.topic === undefined || topic(data.topic)); break
    case 'voc2/defects': valid = isCount(data.total) && Array.isArray(data.items) && data.items.every(item => evidence(item)
      && isRecord(item) && isRecord(item.domain) && isRecord(item.domain.diagnosis) && item.domain.diagnosis.status === 'defect_confirmed'); break
    case 'voc2/jobs': valid = Array.isArray(data.items) && data.items.every(job); break
    case 'voc2/run': valid = job(data.job); break
    case 'voc2/feishu/preview': valid = ['title', 'start', 'end', 'fingerprint'].every(key => typeof data[key] === 'string')
      && typeof data.publish_configured === 'boolean' && isCount(data.count) && isCount(data.total) && data.count <= data.total
      && Array.isArray(data.items) && data.items.every(item => isRecord(item) && typeof item.title === 'string' && typeof item.summary === 'string')
      && (data.destination === undefined || typeof data.destination === 'string')
      && (data.content === undefined || typeof data.content === 'string'); break
    case 'voc2/feishu/publish': valid = httpsLink(data.url) !== undefined && typeof data.title === 'string' && typeof data.reused === 'boolean'
      && (data.count === undefined || isCount(data.count)); break
    case 'voc2/assistant': valid = typeof data.answer === 'string' && typeof data.model === 'string'
      && isRecord(data.scope) && typeof data.scope.label === 'string'
      && (data.answer_mode === undefined || typeof data.answer_mode === 'string' && ['grounded_model', 'computed', 'source_reference', 'insufficient'].includes(data.answer_mode))
      && (data.warnings === undefined || isStringArray(data.warnings))
      && Array.isArray(data.tools) && data.tools.every(item => isRecord(item) && typeof item.name === 'string' && typeof item.label === 'string')
      && Array.isArray(data.citations) && data.citations.every(item => isRecord(item)
        && ['topic', 'knowledge', 'event'].includes(String(item.kind)) && typeof item.id === 'string' && typeof item.title === 'string'); break
  }
  if (!valid) throw new ApiError('VOC 工作台返回的数据格式不完整，请刷新或联系管理员。', 502)
}

function isPair(value: unknown): boolean {
  return isRecord(value) && (typeof value.qa_id === 'number' || typeof value.qa_id === 'string')
    && typeof value.question === 'string' && typeof value.answer === 'string'
}

function isCluster(value: unknown): boolean {
  return isRecord(value) && typeof value.question === 'string' && isStringArray(value.samples)
    && hasCounts(value, ['cluster_id', 'count', 'variants', 'customers', 'groups', 'unanswered', 'share'])
}

function isVocTopic(value: unknown): boolean {
  return isRecord(value) && isCount(value.id) && typeof value.question === 'string'
    && typeof value.theme === 'string' && typeof value.boundary === 'string'
    && ['pending', 'confirmed', 'ignored'].includes(String(value.status))
    && hasCounts(value, ['count', 'customers', 'pending_count', 'revision'])
    && (value.answered_count === undefined || isCount(value.answered_count))
    && Number(value.pending_count) <= Number(value.count)
    && typeof value.first_seen === 'string' && typeof value.last_seen === 'string'
    && isStringArray(value.samples)
}

function isVocJob(value: unknown): boolean {
  return isRecord(value) && hasCounts(value, ['id', 'total', 'processed', 'failed', 'skipped', 'usage_tokens'])
    && ['queued', 'running', 'completed', 'partial', 'failed'].includes(String(value.status))
    && typeof value.model === 'string' && typeof value.error === 'string'
    && typeof value.created_at === 'string' && typeof value.updated_at === 'string'
}

function isKnowledge(value: unknown): boolean {
  return isRecord(value) && hasCounts(value, ['id', 'topic_id', 'revision'])
    && ['question', 'answer', 'scope', 'model', 'created_at', 'updated_at'].every(key => typeof value[key] === 'string')
    && ['draft', 'confirmed', 'ignored'].includes(String(value.status))
    && isStringArray(value.cautions) && isStringArray(value.missing_info)
    && Array.isArray(value.evidence_ids) && value.evidence_ids.every(isCount)
    && Array.isArray(value.reply_patterns) && value.reply_patterns.every(item => isRecord(item)
      && typeof item.label === 'string' && typeof item.text === 'string' && Array.isArray(item.qa_ids) && item.qa_ids.every(isCount))
    && Array.isArray(value.conflicts) && value.conflicts.every(item => isRecord(item)
      && typeof item.description === 'string' && Array.isArray(item.qa_ids) && item.qa_ids.every(isCount))
    && (value.source_quotes === undefined || Array.isArray(value.source_quotes) && value.source_quotes.every(item => isRecord(item) && isCount(item.qa_id) && typeof item.quote === 'string'))
}

function isKnowledgeJob(value: unknown): boolean {
  return isVocJob(value) && isRecord(value) && isCount(value.topic_id)
    && (value.knowledge_id === undefined || value.knowledge_id === null || isCount(value.knowledge_id))
}

function validateVocResponse(name: string, data: unknown): void {
  if (!name.startsWith('voc/')) return
  let valid = isRecord(data)
  if (valid && isRecord(data)) switch (name) {
    case 'voc/summary':
      valid = typeof data.configured === 'boolean' && typeof data.model === 'string'
        && typeof data.prompt_version === 'string'
        && hasCounts(data, ['total_pairs', 'pending_pairs', 'processed_pairs', 'topic_count', 'pending_topics', 'confirmed_topics', 'ignored_topics'])
        && (data.answered_pairs === undefined || isCount(data.answered_pairs))
        && (data.latest_job === null || isVocJob(data.latest_job))
      break
    case 'voc/topics':
      valid = isCount(data.total) && Array.isArray(data.items) && data.items.every(isVocTopic)
      break
    case 'voc/detail':
      valid = isVocTopic(data.topic) && isCount(data.total) && Array.isArray(data.evidence)
        && data.evidence.every(item => isRecord(item) && isCount(item.qa_id)
          && ['question', 'answer', 'customer_name', 'customer_id', 'group_name', 'staff_name', 'asked_at_text', 'reason'].every(key => typeof item[key] === 'string')
          && typeof item.confidence === 'number' && Number.isFinite(item.confidence)
          && ['pending', 'confirmed'].includes(String(item.status)))
      break
    case 'voc/jobs':
      valid = Array.isArray(data.items) && data.items.every(isVocJob)
      break
    case 'voc/run':
      valid = isVocJob(data.job)
      break
    case 'voc/topic':
    case 'voc/merge':
    case 'voc/evidence':
      valid = isVocTopic(data.topic)
      break
    case 'voc/knowledge':
      valid = isCount(data.total) && Array.isArray(data.items) && data.items.every(isKnowledge)
      break
    case 'voc/knowledge/detail':
      valid = isKnowledge(data.knowledge) && Array.isArray(data.evidence) && data.evidence.every(isPair)
      break
    case 'voc/knowledge/jobs':
      valid = Array.isArray(data.items) && data.items.every(isKnowledgeJob)
      break
    case 'voc/knowledge/run':
      valid = isKnowledgeJob(data.job)
      break
    case 'voc/knowledge/save':
      valid = isKnowledge(data.knowledge)
      break
  }
  if (!valid) throw new ApiError('VOC 接口返回的数据格式不完整，请刷新或联系管理员。', 502)
}

/** Reject an older/incompatible backend instead of rendering missing fields as zero. */
function hasDirectoryPage(data: Record<string, unknown>): boolean {
  return hasCounts(data, ['total', 'offset', 'limit']) && isRecord(data.totals) && hasCounts(data.totals, ['questions', 'unanswered'])
}

function validateGetResponse(name: string, data: unknown, filters: ApiFilters): void {
  const paged = filters.limit !== undefined || filters.offset !== undefined || filters.entity_q !== undefined || filters.entity_sort !== undefined
  let valid = true
  if (['summary', 'meta', 'trend', 'pairs', 'questions', 'clusters', 'groups', 'customers', 'detail', 'status'].includes(name)) {
    if (!isRecord(data)) valid = false
    else switch (name) {
      case 'summary':
        valid = hasCounts(data, ['qa_pair', 'unanswered', 'customer_count', 'group_count', 'cluster_count'])
          && Array.isArray(data.recent) && data.recent.every(isPair)
          && Array.isArray(data.top) && data.top.every(isCluster)
        break
      case 'meta':
        valid = isStringArray(data.groups) && isStringArray(data.staff) && isCount(data.total)
        break
      case 'trend':
        valid = Array.isArray(data.series) && data.series.every(point => isRecord(point)
          && typeof point.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(point.date)
          && !Number.isNaN(new Date(`${point.date}T00:00:00Z`).getTime()) && isCount(point.questions))
        break
      case 'pairs':
        valid = isCount(data.total) && Array.isArray(data.items) && data.items.every(isPair)
        break
      case 'questions':
        valid = Array.isArray(data.items) && data.items.every(isCluster)
        break
      case 'clusters': {
        const pagedClusters = paged || filters.cluster_members !== undefined
        valid = Array.isArray(data.items) && data.items.every(item => isCluster(item)
          && (!pagedClusters || (isRecord(item) && typeof item.first_seen === 'string' && typeof item.last_seen === 'string'
            && (item.matched_sample === undefined || typeof item.matched_sample === 'string'))))
          && (!pagedClusters || hasDirectoryPage(data))
        break
      }
      case 'groups':
        valid = Array.isArray(data.items) && data.items.every(item => isRecord(item)
          && typeof item.group_name === 'string' && (!paged || typeof item.last_seen === 'string') && hasCounts(item, ['total_questions', 'customer_count', 'unanswered'])
          && Array.isArray(item.top) && item.top.every(top => isRecord(top) && typeof top.question === 'string'
            && hasCounts(top, ['cluster_id', 'count'])))
        valid &&= !paged || hasDirectoryPage(data)
        break
      case 'customers':
        valid = Array.isArray(data.items) && data.items.every(item => isRecord(item)
          && typeof item.customer_id === 'string' && typeof item.group_name === 'string'
          && typeof item.last_seen === 'string'
          && (!paged || typeof item.customer_name === 'string' && isStringArray(item.group_names) && isCount(item.group_count))
          && hasCounts(item, ['total_questions', 'distinct_clusters', 'unanswered']))
        valid &&= !paged || hasDirectoryPage(data)
        break
      case 'detail':
        valid = isPair(data.pair) && typeof data.archived === 'boolean'
          && validContextRevision(data.context_revision)
          && Array.isArray(data.messages) && data.messages.every(message => isRecord(message)
            && typeof message.role === 'string' && typeof message.content === 'string' && typeof message.time === 'string'
            && (message.ts === undefined || isCount(message.ts))
            && (message.source_message_id === undefined || message.source_message_id === null || typeof message.source_message_id === 'string' || typeof message.source_message_id === 'number')
            && (message.is_question === undefined || typeof message.is_question === 'boolean')
            && (message.is_answer === undefined || typeof message.is_answer === 'boolean')
            && (message.context_seq === undefined || validContextSequence(message.context_seq))
            && validSenderMetadata(message)
            && validDetailImages(message.images))
          && (data.message_count === undefined || isCount(data.message_count) && data.message_count === (data.messages as unknown[]).length)
          && (data.availability === undefined || ['available', 'partial', 'archived', 'empty'].includes(String(data.availability)))
          && (data.context_scope === undefined || ['session_customer', 'legacy_session'].includes(String(data.context_scope)))
          && (data.can_lookup_replies === undefined || typeof data.can_lookup_replies === 'boolean')
        break
      case 'status':
        valid = hasCounts(data, ['sessions', 'total_pairs']) && Array.isArray(data.tasks)
          && data.tasks.every(task => isRecord(task) && typeof task.kind === 'string'
            && typeof task.created_at_text === 'string' && isRecord(task.result))
        break
    }
  }
  if (!valid) throw new ApiError('服务接口版本不匹配，请刷新或联系管理员', 502)
}

function validateDetailRepliesResponse(data: unknown): void {
  const valid = isRecord(data) && detailRepliesStatuses.some(status => status === data.status)
    && validContextRevision(data.context_revision)
    && Array.isArray(data.messages) && (data.status === 'found' ? data.messages.length > 0 : data.messages.length === 0)
    && data.messages.every(message => isRecord(message) && message.role === 'staff'
      && (message.sender_name === undefined || typeof message.sender_name === 'string')
      && typeof message.content === 'string' && Boolean(message.content.trim())
      && isCount(message.ts) && message.ts > 0
      && typeof message.time === 'string' && Boolean(message.time.trim())
      && typeof message.source_message_id === 'string' && Boolean(message.source_message_id.trim())
      && message.is_question === false && message.is_answer === false
      && validDetailImages(message.images))
    && new Set(data.messages.map(message => message.source_message_id)).size === data.messages.length
    && (data.senders === undefined || Array.isArray(data.senders)
      && data.senders.every(sender => isRecord(sender) && validContextSequence(sender.context_seq) && validSenderMetadata(sender))
      && new Set(data.senders.map(sender => sender.context_seq)).size === data.senders.length)
  if (!valid) throw new ApiError('七鱼回复接口返回的数据格式不完整，请稍后重试。', 502)
}

function validateImportResponse(name: string, data: unknown): void {
  if (name === 'import/preview') {
    const valid = isRecord(data) && typeof data.filename === 'string' && typeof data.kind === 'string'
      && typeof data.digest === 'string' && /^[a-f0-9]{64}$/.test(data.digest) && typeof data.can_import === 'boolean'
      && isRecord(data.fields) && Object.values(data.fields).every(value => typeof value === 'string')
      && hasCounts(data, ['total_rows', 'valid_rows', 'duplicate_rows', 'skipped_rows', 'sessions', 'estimated_pairs', 'unknown_time'])
      && Array.isArray(data.errors) && data.errors.every(error => isRecord(error) && isCount(error.row) && typeof error.reason === 'string')
      && Array.isArray(data.samples) && data.samples.every(sample => isRecord(sample)
        && ['question', 'answer', 'customer_name', 'group_name'].every(field => typeof sample[field] === 'string'))
    if (!valid) throw new ApiError('导入预览返回的数据不完整，请重新预览。', 502)
  } else if (name === 'import' && !(isRecord(data) && hasCounts(data, ['new_sessions', 'updated_sessions', 'qa_pairs']))) {
    throw new ApiError('导入结果返回的数据不完整，请在数据清洗页核对处理记录。', 502)
  }
}

function validateGatewayResponse(name: string, data: unknown): void {
  if (!name.startsWith('gateway/')) return
  let valid = isRecord(data)
  if (valid && isRecord(data)) {
    if (name === 'gateway/config') {
      valid = typeof data.base_url === 'string' && typeof data.model === 'string'
        && typeof data.key_configured === 'boolean' && typeof data.configured === 'boolean'
        && Number.isInteger(data.timeout) && Number(data.timeout) > 0
        && Number.isInteger(data.batch_size) && Number(data.batch_size) > 0
    } else if (name === 'gateway/models') {
      valid = isCount(data.total) && Array.isArray(data.items)
        && data.items.every(item => isRecord(item) && typeof item.id === 'string' && item.id.length > 0)
    }
  }
  if (!valid) throw new ApiError('模型网关设置返回的数据不完整，请刷新后重试。', 502)
}

export async function apiGet<T>(name: string, filters: ApiFilters, signal?: AbortSignal, activity: { background?: boolean; prefetch?: boolean } = {}): Promise<T> {
  const read = async () => {
    const data = await readResponse<unknown>(await fetch(apiUrl(name, filters), {
      credentials: 'same-origin', cache: 'no-store', signal, ...(activity.prefetch ? { priority: 'low' } : {}),
    }), !activity.prefetch)
    validateGetResponse(name, data, filters)
    validateVocResponse(name, data)
    validateVoc2Response(name, data, filters.workbench === 1)
    if (filters.first_page && (name === 'voc2/issue-pilot' || name === 'voc2/playbooks' || name === 'voc2/complete-sops')) {
      const page = data as VocIssuePilot & Pick<VocPlaybookResultsData, 'case_paths'>
      const rows = page.items.length + (page.case_paths?.length ?? 0)
      const limit = name === 'voc2/issue-pilot' ? 6 : 8
      if (!Number.isSafeInteger(page.first_page_total) || rows !== Math.min(page.first_page_total ?? 0, limit)) {
        throw new ApiError('首页返回的数据不完整，请重试。', 502)
      }
    }
    validateGatewayResponse(name, data)
    return data as T
  }
  return activity.prefetch ? read() : withRequestActivity('正在读取数据…', read, activity.background)
}

export async function apiPost<T>(name: string, filters: ApiFilters, body?: BodyInit, headers?: HeadersInit, signal?: AbortSignal): Promise<T> {
  const replyLookup = name === 'detail/replies'
  const label = replyLookup ? '正在查询七鱼客服回复…' : name === 'voc2/assistant' ? '正在查询会话数据…' : name === 'voc2/feishu/publish' ? '正在发布飞书成果…' : name === 'clean' ? '正在清洗会话…' : name.startsWith('import') ? '正在处理导入文件…' : name === 'gateway/models' ? '正在读取模型列表…' : name.includes('run') ? '正在提交整理任务…' : '正在保存…'
  return withRequestActivity(label, async () => {
    const requestHeaders = new Headers(headers)
    if (!requestHeaders.has('Content-Type')) requestHeaders.set('Content-Type', 'application/octet-stream')
    requestHeaders.set('X-Local-Request', '1')
    const data = await readResponse<unknown>(await fetch(apiUrl(name, filters), {
      method: 'POST', credentials: 'same-origin', headers: requestHeaders,
      body: body ?? new Uint8Array(), signal,
    }))
    validateVocResponse(name, data)
    validateVoc2Response(name, data)
    validateGatewayResponse(name, data)
    if (replyLookup) validateDetailRepliesResponse(data)
    validateImportResponse(name, data)
    return data as T
  }, replyLookup)
}

export interface GatewayConfig {
  base_url: string
  model: string
  key_configured: boolean
  configured: boolean
  timeout: number
  batch_size: number
}

export interface GatewayModels { items: { id: string }[]; total: number }

async function downloadFile(name: string, filters: ApiFilters, filename: string, label: string): Promise<void> {
  return withRequestActivity(label, async () => {
    const response = await fetch(apiUrl(name, filters), { credentials: 'same-origin', cache: 'no-store' })
    if (!response.ok) await readResponse<never>(response)
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  })
}

export function downloadExport(filters: ApiFilters): Promise<void> {
  return downloadFile('export', filters, '语海Voc-脱敏问答.csv', '正在导出问答…')
}

export function downloadKnowledgeExport(source: ApiFilters['source']): Promise<void> {
  return downloadFile('voc/knowledge/export', { source, status: 'confirmed' }, '语海Voc-知识导出.csv', '正在导出知识…')
}

export function downloadTemplate(): Promise<void> {
  return downloadFile('template', { source: 'local' }, '语海Voc-会话导入模板.csv', '正在下载导入模板…')
}

export function normalizePair(raw: Record<string, unknown>): QAPair {
  const timestamp = Number(raw.asked_at)
  const shifted = new Date(timestamp + 8 * 60 * 60 * 1000)
  const askedAt = Number.isFinite(timestamp) && timestamp > 0 && !Number.isNaN(shifted.getTime())
    ? `${shifted.toISOString().slice(0, 19)}+08:00`
    : ''
  return {
    id: String(raw.qa_id ?? raw.id ?? ''),
    sessionId: String(raw.session_id ?? ''),
    clusterId: Number(raw.cluster_id) || 0,
    question: String(raw.question ?? ''),
    answer: String(raw.answer ?? ''),
    customerName: String(raw.customer_name || '匿名客户'),
    customerId: String(raw.customer_id ?? ''),
    groupName: String(raw.group_name || '未分组'),
    staffName: String(raw.staff_name || '未记录客服'),
    askedAt,
    archived: raw.archived === true,
  }
}

export interface ClusterItem {
  cluster_id: number
  question: string
  rep?: string
  size?: number
  count: number
  variants: number
  customers: number
  groups: number
  unanswered: number
  samples: string[]
  matched_sample?: string
  share: number
  first_seen: string
  last_seen: string
}

export interface SummaryData {
  qa_pair: number
  customer_count: number
  group_count: number
  cluster_count: number
  unanswered: number
  unknown_time: number
  first_seen: string
  last_seen: string
  top: ClusterItem[]
  recent: Record<string, unknown>[]
  raw_session: number
  raw_message: number
  clean_result: number
  local?: Record<string, unknown>
}

export interface MetaData {
  groups: string[]
  staff: string[]
  mock: boolean
  db_name: string
  total: number
  unknown_time: number
  source: 'local'
}

export interface TrendData {
  days: number
  series: { date: string; questions: number }[]
  truncated: boolean
  unknown_time: number
}

export interface LocalStatus {
  sessions: number
  messages?: number
  pending?: number
  completed?: number
  no_questions?: number
  no_body?: number
  failed?: number
  version?: string
  total_pairs: number
  tasks: { id?: number; kind: string; status?: string; created_at_text: string; result: Record<string, unknown> }[]
  [key: string]: unknown
}

export type VocStatus = 'pending' | 'confirmed' | 'ignored'
export interface VocTopic {
  id: number; question: string; theme: string; boundary: string; status: VocStatus
  count: number; customers: number; pending_count: number; answered_count?: number
  first_seen: string; last_seen: string; samples: string[]; revision: number
}
export interface VocJob {
  id: number; status: 'queued' | 'running' | 'completed' | 'partial' | 'failed'
  total: number; processed: number; failed: number; skipped: number
  model: string; error: string; created_at: string; updated_at: string; usage_tokens: number
}
export interface VocSummary {
  configured: boolean; model: string; prompt_version: string
  total_pairs: number; pending_pairs: number; processed_pairs: number; answered_pairs?: number
  topic_count: number; pending_topics: number; confirmed_topics: number; ignored_topics: number
  latest_job: VocJob | null
}
export interface VocEvidence extends Record<string, unknown> {
  qa_id: number; question: string; answer: string
  customer_name: string; customer_id: string; group_name: string; staff_name: string
  asked_at_text: string; confidence: number; reason: string; status: 'pending' | 'confirmed'
}
export interface VocTopicList { items: VocTopic[]; total: number }
export interface VocDetail { topic: VocTopic; evidence: VocEvidence[]; total: number }

export type KnowledgeStatus = 'draft' | 'confirmed' | 'ignored'
export interface Knowledge {
  id: number; topic_id: number; question: string; answer: string; scope: string; cautions: string[]
  reply_patterns: { label: string; text: string; qa_ids: number[] }[]
  conflicts: { description: string; qa_ids: number[] }[]
  missing_info: string[]; evidence_ids: number[]; status: KnowledgeStatus; revision: number
  model: string; created_at: string; updated_at: string
  source_quotes?: { qa_id: number; quote: string }[]
}
export interface KnowledgeJob extends VocJob { topic_id: number; knowledge_id?: number | null }
export interface KnowledgeDetail { knowledge: Knowledge; evidence: Record<string, unknown>[] }

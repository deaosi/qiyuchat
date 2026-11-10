import type { QAPair } from '@/lib/demo-data'

export interface ApiFilters {
  source: 'local' | 'demo'
  days?: number
  start?: string
  end?: string
  group?: string
  staff?: string
  cluster?: string | number
  customer?: string
  q?: string
  replied?: string
  limit?: number
  offset?: number
  id?: string | number
  top?: number
  status?: VocStatus | KnowledgeStatus | 'all'
  topic_q?: string
  evidence_status?: 'all' | 'pending' | 'confirmed'
  topic_id?: number
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
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })
  return `/api/${name}?${params}`
}

async function readResponse<T>(response: Response): Promise<T> {
  if (response.status === 401) {
    if (window.location.pathname !== '/login') window.location.assign('/login')
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

function hasCounts(value: Record<string, unknown>, keys: string[]): boolean {
  return keys.every(key => isCount(value[key]))
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === 'string')
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
function validateGetResponse(name: string, data: unknown): void {
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
      case 'clusters':
        valid = Array.isArray(data.items) && data.items.every(isCluster)
        break
      case 'groups':
        valid = Array.isArray(data.items) && data.items.every(item => isRecord(item)
          && typeof item.group_name === 'string' && hasCounts(item, ['total_questions', 'customer_count', 'unanswered'])
          && Array.isArray(item.top) && item.top.every(top => isRecord(top) && typeof top.question === 'string'
            && hasCounts(top, ['cluster_id', 'count'])))
        break
      case 'customers':
        valid = Array.isArray(data.items) && data.items.every(item => isRecord(item)
          && typeof item.customer_id === 'string' && typeof item.group_name === 'string'
          && typeof item.last_seen === 'string' && hasCounts(item, ['total_questions', 'distinct_clusters', 'unanswered']))
        break
      case 'detail':
        valid = isPair(data.pair) && typeof data.archived === 'boolean'
          && Array.isArray(data.messages) && data.messages.every(message => isRecord(message)
            && typeof message.role === 'string' && typeof message.content === 'string' && typeof message.time === 'string')
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

export async function apiGet<T>(name: string, filters: ApiFilters, signal?: AbortSignal): Promise<T> {
  const data = await readResponse<unknown>(await fetch(apiUrl(name, filters), {
    credentials: 'same-origin', cache: 'no-store', signal,
  }))
  validateGetResponse(name, data)
  validateVocResponse(name, data)
  validateGatewayResponse(name, data)
  return data as T
}

export async function apiPost<T>(name: string, filters: ApiFilters, body?: BodyInit, headers?: HeadersInit): Promise<T> {
  const requestHeaders = new Headers(headers)
  if (!requestHeaders.has('Content-Type')) requestHeaders.set('Content-Type', 'application/octet-stream')
  requestHeaders.set('X-Local-Request', '1')
  const data = await readResponse<unknown>(await fetch(apiUrl(name, filters), {
    method: 'POST', credentials: 'same-origin', headers: requestHeaders,
    body: body ?? new Uint8Array(),
  }))
  validateVocResponse(name, data)
  validateGatewayResponse(name, data)
  return data as T
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

export async function downloadExport(filters: ApiFilters): Promise<void> {
  const response = await fetch(apiUrl('export', filters), { credentials: 'same-origin', cache: 'no-store' })
  if (!response.ok) await readResponse<never>(response)
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = filters.source === 'demo' ? '七鱼问答-演示数据.csv' : '七鱼问答-脱敏导出.csv'
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function downloadKnowledgeExport(source: ApiFilters['source']): Promise<void> {
  const response = await fetch(apiUrl('voc/knowledge/export', { source, status: 'confirmed' }), { credentials: 'same-origin', cache: 'no-store' })
  if (!response.ok) await readResponse<never>(response)
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = source === 'demo' ? '七鱼知识-已确认-演示库.csv' : '七鱼知识-已确认.csv'
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
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
  source: 'local' | 'demo'
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

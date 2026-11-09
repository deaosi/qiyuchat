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

export async function apiGet<T>(name: string, filters: ApiFilters, signal?: AbortSignal): Promise<T> {
  const data = await readResponse<unknown>(await fetch(apiUrl(name, filters), {
    credentials: 'same-origin', cache: 'no-store', signal,
  }))
  validateGetResponse(name, data)
  return data as T
}

export async function apiPost<T>(name: string, filters: ApiFilters, body?: BodyInit, headers?: HeadersInit): Promise<T> {
  const requestHeaders = new Headers(headers)
  if (!requestHeaders.has('Content-Type')) requestHeaders.set('Content-Type', 'application/octet-stream')
  requestHeaders.set('X-Local-Request', '1')
  return readResponse<T>(await fetch(apiUrl(name, filters), {
    method: 'POST', credentials: 'same-origin', headers: requestHeaders,
    body: body ?? new Uint8Array(),
  }))
}

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

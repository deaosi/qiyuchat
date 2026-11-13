import { apiPost, type ApiFilters, type VocQueryFilters } from '@/lib/api'

export type ChatCitation = { kind: 'topic' | 'knowledge' | 'event'; id: string; title: string }
export type ChatExample = {
  citation: ChatCitation & { kind: 'event' }
  question: string
  reply: string
  time?: string
  outcome?: string
}
export type ChatAnswerMode = 'grounded_model' | 'grounded_selection' | 'computed' | 'source_reference' | 'insufficient'
export type ChatAnswer = { answer: string; citations: ChatCitation[]; examples?: ChatExample[]; scope: { label: string; start?: string; end?: string; query_filters?: VocQueryFilters }; model: string; tools: { name: string; label: string }[]; answer_mode?: ChatAnswerMode; warnings?: string[] }
export type ChatMessage = { id: string; role: 'user' | 'assistant'; content: string; citations?: ChatCitation[]; examples?: ChatExample[]; scope?: string; queryFilters?: VocQueryFilters; model?: string; tools?: { name: string; label: string }[]; answerMode?: ChatAnswerMode; warnings?: string[] }
export type ChatThread = { id: string; title: string; filters: ApiFilters; messages: ChatMessage[]; draft: string; updatedAt: number; error?: string }
export type ChatContext = { query_filters: VocQueryFilters; citations: Pick<ChatCitation, 'kind' | 'id'>[] }
export type ChatRequest = { message: string; history: { role: 'user' | 'assistant'; content: string }[]; context?: ChatContext }
type ChatSnapshot = { threads: ChatThread[]; activeId: string; pending: Record<string, boolean>; storageWarning: boolean }
type Transport = (filters: ApiFilters, body: ChatRequest, signal: AbortSignal) => Promise<ChatAnswer>
type StorageLike = Pick<Storage, 'getItem' | 'setItem'>
export const CHAT_STORAGE_KEY = 'qiyu-voc-chat-v1'
const projectionKeys = ['days', 'start', 'end', 'group', 'staff', 'customer', 'q', 'support_intent', 'diagnosis_status', 'replied', 'cluster', 'focus_topic'] as const
const intents = ['all', 'configuration_guidance', 'functional_anomaly', 'unclear'] as const
const diagnoses = ['all', 'reported', 'pending', 'config_cause_confirmed', 'platform_limit_confirmed', 'defect_confirmed', 'not_applicable'] as const
const answerModes = ['grounded_model', 'grounded_selection', 'computed', 'source_reference', 'insufficient'] as const
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown, maximum = 500) => typeof value === 'string' ? value.slice(0, maximum) : ''

function readExamples(value: unknown): ChatExample[] | undefined {
  if (!Array.isArray(value)) return undefined
  const examples: ChatExample[] = []
  const seen = new Set<string>()
  for (const item of value.slice(0, 5)) {
    if (!record(item) || !record(item.citation) || item.citation.kind !== 'event') continue
    const { id } = item.citation
    if (typeof id !== 'string' || id.length > 200 || !id.trim() || id.trim() !== id || /[\u0000-\u001f\u007f]/.test(id) || seen.has(id)) continue
    const title = text(item.citation.title, 200).trim()
    const question = text(item.question, 2000).trim()
    const reply = text(item.reply, 2000).trim()
    if (!title || !question || !reply) continue
    const time = text(item.time, 80).trim()
    const outcome = text(item.outcome, 2000).trim()
    examples.push({
      citation: { kind: 'event', id, title }, question, reply,
      ...(time ? { time } : {}), ...(outcome ? { outcome } : {}),
    })
    seen.add(id)
  }
  return examples.length ? examples : undefined
}

export function normalizeChatFilters(value: unknown): ApiFilters {
  const raw = record(value) ? value : {}
  const validDate = (value: unknown) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value ? value : ''
  const start = validDate(raw.start), end = validDate(raw.end)
  const n = raw.days === null ? 0 : Number(raw.days ?? 7)
  const cluster = Number(raw.cluster)
  const focusTopic = typeof raw.focus_topic === 'string' && /^t_[a-f0-9]{24}$/.test(raw.focus_topic) ? raw.focus_topic : undefined
  const days = start || end ? 0 : Number.isInteger(n) && n >= 0 && n <= 365 ? n : 7
  return { source: 'local', days, start, end, group: text(raw.group), staff: text(raw.staff), customer: text(raw.customer), q: text(raw.q),
    replied: ['answered', 'unanswered'].includes(String(raw.replied)) ? String(raw.replied) : '',
    cluster: Number.isSafeInteger(cluster) && cluster > 0 ? cluster : 0,
    support_intent: intents.includes(raw.support_intent as typeof intents[number]) ? raw.support_intent as ApiFilters['support_intent'] : 'all',
    diagnosis_status: diagnoses.includes(raw.diagnosis_status as typeof diagnoses[number]) ? raw.diagnosis_status as ApiFilters['diagnosis_status'] : 'all',
    ...(focusTopic ? { focus_topic: focusTopic } : {}) }
}
export const chatScopeKey = (filters: ApiFilters) => JSON.stringify(normalizeChatFilters(filters))
export function chatHref(filters: ApiFilters): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(normalizeChatFilters(filters))) {
    if (key !== 'source' && value !== '' && value !== 'all' && (key === 'days' || value !== 0)) params.set(key, String(value))
  }
  return '#voc-chat?' + params.toString()
}
export function readChatFilters(hash: string): ApiFilters | undefined {
  if (hash.split('?')[0] !== '#voc-chat' || !hash.includes('?')) return undefined
  return normalizeChatFilters(Object.fromEntries(new URLSearchParams(hash.slice(hash.indexOf('?') + 1))))
}
export function citationFilters(base: ApiFilters, projection?: VocQueryFilters): ApiFilters {
  if (!projection) return normalizeChatFilters(base)
  const allowed = Object.fromEntries(projectionKeys.filter(key => Object.hasOwn(projection, key)).map(key => [key, projection[key] ?? (key === 'days' ? 0 : '')]))
  return normalizeChatFilters({ ...base, ...allowed, focus_topic: base.focus_topic })
}
function readProjection(value: Record<string, unknown>): VocQueryFilters {
  const normalized = normalizeChatFilters(value)
  return Object.fromEntries(projectionKeys.filter(key => Object.hasOwn(value, key)).map(key => [key, normalized[key]])) as VocQueryFilters
}

/** Previous output is a retrieval hint, never an instruction or an authoritative fact. */
function previousContext(messages: ChatMessage[]): ChatContext | undefined {
  const last = [...messages].reverse().find(message => message.role === 'assistant')
  if (!record(last?.queryFilters)) return undefined
  const query_filters = readProjection(last.queryFilters)
  if (!Object.keys(query_filters).length) return undefined
  const citations: ChatContext['citations'] = [], seen = new Set<string>()
  for (const ref of last.citations || []) {
    if (!record(ref) || !['topic', 'knowledge', 'event'].includes(String(ref.kind)) || typeof ref.id !== 'string' || !ref.id.trim() || ref.id.length > 200) continue
    const key = `${ref.kind}:${ref.id}`
    if (seen.has(key)) continue
    seen.add(key); citations.push({ kind: ref.kind as ChatCitation['kind'], id: ref.id })
    if (citations.length === 10) break
  }
  return { query_filters, citations }
}
export function chatScopeLabel(filters: ApiFilters): string {
  const f = normalizeChatFilters(filters)
  return [f.start || f.end ? `${f.start || '不限'} 至 ${f.end || '不限'}` : f.days ? `最近 ${f.days} 天` : '全部时间',
    f.group ? `客户群：${f.group}` : '全部客户群', f.staff ? `客服：${f.staff}` : '全部客服', f.customer ? '指定客户' : '',
    f.support_intent === 'configuration_guidance' ? '配置与使用' : f.support_intent === 'functional_anomaly' ? '功能异常与排障' : '',
    f.diagnosis_status && f.diagnosis_status !== 'all' ? '已限定诊断状态' : '', f.focus_topic ? '仅当前问题' : ''].filter(Boolean).join(' · ')
}

function loadHistory(storage?: StorageLike): { threads: ChatThread[]; activeId: string } {
  try {
    const raw = storage?.getItem(CHAT_STORAGE_KEY)
    if (!raw || raw.length > 2000000) return { threads: [], activeId: '' }
    const parsed: unknown = JSON.parse(raw)
    if (!record(parsed) || parsed.version !== 1 || !Array.isArray(parsed.threads)) return { threads: [], activeId: '' }
    const threads: ChatThread[] = []
    for (const item of parsed.threads.slice(0, 20)) {
      if (!record(item) || typeof item.id !== 'string' || typeof item.title !== 'string' || !Array.isArray(item.messages) || !Number.isFinite(item.updatedAt)) continue
      const messages: ChatMessage[] = []
      let valid = true
      for (const msg of item.messages) {
        if (!record(msg) || typeof msg.id !== 'string' || !['user', 'assistant'].includes(String(msg.role)) || typeof msg.content !== 'string' || msg.content.length > 200000) { valid = false; break }
        const citations: ChatCitation[] = []
        if (Array.isArray(msg.citations)) for (const ref of msg.citations) {
          if (record(ref) && ['topic', 'knowledge', 'event'].includes(String(ref.kind)) && typeof ref.id === 'string' && typeof ref.title === 'string') citations.push({ kind: ref.kind as ChatCitation['kind'], id: ref.id, title: ref.title })
        }
        messages.push({ id: msg.id, role: msg.role as ChatMessage['role'], content: msg.content, citations,
          examples: msg.role === 'assistant' ? readExamples(msg.examples) : undefined,
          scope: text(msg.scope, 2000), model: text(msg.model), queryFilters: record(msg.queryFilters) ? readProjection(msg.queryFilters) : undefined,
          answerMode: answerModes.includes(msg.answerMode as ChatAnswerMode) ? msg.answerMode as ChatAnswerMode : undefined,
          warnings: Array.isArray(msg.warnings) && msg.warnings.every(item => typeof item === 'string') ? msg.warnings : undefined,
          tools: Array.isArray(msg.tools) ? msg.tools.filter(record).filter(t => typeof t.name === 'string' && typeof t.label === 'string').map(t => ({ name: String(t.name), label: String(t.label) })) : [] })
      }
      if (!valid) continue
      let draft = text(item.draft, 2000), error = ''
      if (item.waiting === true && messages.at(-1)?.role === 'user') { draft = messages.pop()!.content.slice(0, 2000); error = '上一条查询在页面关闭或刷新时中断，可以重新发送。' }
      threads.push({ id: item.id, title: item.title.slice(0, 100), filters: normalizeChatFilters(item.filters), messages, draft, updatedAt: Number(item.updatedAt), error })
    }
    return { threads, activeId: threads.some(t => t.id === parsed.activeId) ? String(parsed.activeId) : threads[0]?.id || '' }
  } catch { return { threads: [], activeId: '' } }
}

export function createChatStore(options: { storage?: StorageLike; transport?: Transport; now?: () => number; id?: () => string } = {}) {
  const now = options.now || Date.now, id = options.id || (() => crypto.randomUUID())
  const transport: Transport = options.transport || ((filters, body, signal) => apiPost<ChatAnswer>('voc2/assistant', filters, JSON.stringify(body), { 'Content-Type': 'application/json' }, signal))
  let state: ChatSnapshot = { ...loadHistory(options.storage), pending: {}, storageWarning: false }
  const listeners = new Set<() => void>()
  const requests = new Map<string, { controller: AbortController; messageId: string; text: string }>()
  const empty = (filters: ApiFilters): ChatThread => ({ id: id(), title: '新对话', filters: normalizeChatFilters(filters), messages: [], draft: '', updatedAt: now() })
  function publish(next: ChatSnapshot) {
    state = next
    try {
      if (options.storage) {
        let saved = [...state.threads.filter(t => t.id === state.activeId), ...state.threads.filter(t => t.id !== state.activeId)].slice(0, 20)
        let serialized = ''
        do {
          serialized = JSON.stringify({ version: 1, activeId: state.activeId, threads: saved.map(t => ({ ...t, waiting: !!state.pending[t.id] })) })
          if (serialized.length <= 1800000) break
          let index = saved.length - 1
          while (index >= 0 && (saved[index].id === state.activeId || state.pending[saved[index].id])) index--
          if (index < 0) throw new Error('history_capacity')
          saved = saved.filter((_, i) => i !== index)
        } while (saved.length)
        options.storage.setItem(CHAT_STORAGE_KEY, serialized)
      }
      state = { ...state, storageWarning: !options.storage }
    } catch { state = { ...state, storageWarning: true } }
    listeners.forEach(listener => listener())
  }
  const update = (threadId: string, edit: (thread: ChatThread) => ChatThread, pending = state.pending) => publish({ ...state, pending, threads: state.threads.map(t => t.id === threadId ? edit(t) : t).sort((a, b) => b.updatedAt - a.updatedAt) })
  const withoutPending = (threadId: string) => Object.fromEntries(Object.entries(state.pending).filter(([key]) => key !== threadId))
  const create = (filters: ApiFilters) => {
    const thread = empty(filters)
    const kept = state.threads.filter((_, index) => index < 19 || state.pending[state.threads[index].id])
    publish({ ...state, activeId: thread.id, threads: [thread, ...kept] })
    return thread
  }
  const store = {
    getSnapshot: () => state,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } },
    ensureScope(filters: ApiFilters) {
      const current = state.threads.find(t => t.id === state.activeId)
      if (current && chatScopeKey(current.filters) === chatScopeKey(filters)) return current
      const reusable = state.threads.find(t => !t.messages.length && !t.draft && chatScopeKey(t.filters) === chatScopeKey(filters))
      if (reusable) { publish({ ...state, activeId: reusable.id }); return reusable }
      return create(filters)
    },
    newThread: create,
    select(threadId: string) {
      const thread = state.threads.find(t => t.id === threadId)
      if (thread) publish({ ...state, activeId: threadId })
      return thread
    },
    setDraft(threadId: string, draft: string) { update(threadId, t => ({ ...t, draft: draft.slice(0, 2000) })) },
    remove(threadId: string) {
      requests.get(threadId)?.controller.abort(); requests.delete(threadId)
      const current = state.threads.find(t => t.id === threadId)
      const threads = state.threads.filter(t => t.id !== threadId)
      if (!threads.length) threads.push(empty(current?.filters || { source: 'local', days: 7 }))
      publish({ ...state, threads, activeId: state.activeId === threadId ? threads[0].id : state.activeId, pending: withoutPending(threadId) })
      return threads.find(t => t.id === state.activeId)!
    },
    stop(threadId: string) {
      const request = requests.get(threadId)
      if (!request) return
      requests.delete(threadId); request.controller.abort()
      update(threadId, t => ({ ...t, messages: t.messages.filter(m => m.id !== request.messageId), draft: request.text, error: '已停止等待，可以修改问题后重新发送。' }), withoutPending(threadId))
    },
    async send(threadId: string, content: string) {
      const thread = state.threads.find(t => t.id === threadId), question = content.trim()
      if (!thread || requests.has(threadId) || !question || question.length > 2000) return
      const request = { controller: new AbortController(), messageId: id(), text: question }
      const history = thread.messages.filter(message => (message.role === 'user' || message.role === 'assistant') && typeof message.content === 'string').slice(-20).map(({ role, content }) => ({ role, content }))
      const context = previousContext(thread.messages)
      requests.set(threadId, request)
      update(threadId, t => ({ ...t, title: t.messages.length ? t.title : question.slice(0, 42), messages: [...t.messages, { id: request.messageId, role: 'user', content: question }], draft: '', error: '', updatedAt: now() }), { ...state.pending, [threadId]: true })
      try {
        const answer = await transport({ ...thread.filters }, { message: question, history, ...(context ? { context } : {}) }, request.controller.signal)
        if (requests.get(threadId) !== request) return
        requests.delete(threadId)
        const queryFilters = { ...answer.scope.query_filters }
        if (answer.scope.start || answer.scope.end) Object.assign(queryFilters, { days: 0, start: answer.scope.start || '', end: answer.scope.end || '' })
        update(threadId, t => ({ ...t, messages: [...t.messages, {
          id: id(), role: 'assistant', content: answer.answer, citations: answer.citations, examples: readExamples(answer.examples),
          scope: answer.scope.label, queryFilters, model: answer.model, tools: answer.tools, answerMode: answer.answer_mode, warnings: answer.warnings,
        }], updatedAt: now() }), withoutPending(threadId))
      } catch (error) {
        if (requests.get(threadId) !== request) return
        requests.delete(threadId)
        update(threadId, t => ({ ...t, messages: t.messages.filter(m => m.id !== request.messageId), draft: request.text, error: error instanceof Error ? error.message : '查询失败，请重试。' }), withoutPending(threadId))
      }
    },
  }
  return store
}
export type VocChatStore = ReturnType<typeof createChatStore>
let shared: VocChatStore | undefined
export function getVocChatStore(): VocChatStore {
  if (!shared) {
    let storage: Storage | undefined
    try { storage = window.sessionStorage } catch { /* A private browser may disable storage. */ }
    shared = createChatStore({ storage })
  }
  return shared
}

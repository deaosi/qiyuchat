import { afterEach, describe, expect, it, vi } from 'vitest'
import { CHAT_STORAGE_KEY, chatHref, chatScopeKey, citationFilters, createChatStore, normalizeChatFilters, readChatFilters, type ChatAnswer } from './voc-chat'
import { apiPost, type ApiFilters } from './api'

it('preserves the selected topic in links and cannot switch topic through a citation', () => {
  const focus = 't_' + 'a'.repeat(24)
  const other = 't_' + 'b'.repeat(24)
  const scoped = normalizeChatFilters({ days: 7, focus_topic: focus, workbench: 1, trend: 'rising' })

  expect(readChatFilters(chatHref(scoped))).toEqual(scoped)
  expect(scoped).not.toHaveProperty('trend')
  expect(scoped).not.toHaveProperty('workbench')
  expect(citationFilters(scoped, { days: 30, focus_topic: other })).toMatchObject({ days: 30, focus_topic: focus })
  expect(normalizeChatFilters({ focus_topic: 'unbounded' })).not.toHaveProperty('focus_topic')
})
afterEach(() => vi.unstubAllGlobals())

function memoryStorage() {
  const values = new Map<string, string>()
  return { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } }
}
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function factory(transport = vi.fn().mockResolvedValue(answer())) {
  let n = 0
  const storage = memoryStorage()
  return { store: createChatStore({ storage, transport, id: () => `id-${++n}`, now: () => n }), storage, transport }
}
function answer(): ChatAnswer {
  return { answer: '如果使用子账号，请先检查商品权限。', citations: [{ kind: 'event', id: 'event1', title: '权限检查原话' }],
    scope: { label: '2026-11-04 至 2026-11-10 · A群', start: '2026-11-04', end: '2026-11-10', query_filters: { days: 7, group: 'A群', customer: '', q: '' } },
    model: 'configured-model', tools: [{ name: 'search', label: '查找依据' }] }
}
const a: ApiFilters = { source: 'local', days: 7, group: 'A群' }
function thirtyDayAnswer(): ChatAnswer {
  return { ...answer(), scope: { label: '2026-10-12 至 2026-11-10 · A群', start: '2026-10-12', end: '2026-11-10', query_filters: { days: 30, group: 'A群', customer: '', q: '' } } }
}

describe('independent chat scopes and links', () => {
  it('keeps filters when opening a separate tab and rejects unrelated URL fields', () => {
    const filters = { ...a, staff: '客服1', support_intent: 'functional_anomaly' as const, diagnosis_status: 'pending' as const }
    const href = chatHref(filters)
    expect(chatScopeKey(readChatFilters(href)!)).toBe(chatScopeKey(filters))
    const parsed = readChatFilters(href + '&source=demo&api_key=secret&endpoint=https%3A%2F%2Fevil.example')!
    expect(parsed.source).toBe('local')
    expect(parsed).not.toHaveProperty('api_key')
    expect(parsed).not.toHaveProperty('endpoint')
  })

  it('uses the answer scope for citations and clears inherited search/customer conditions', () => {
    const base = { ...a, q: '旧搜索', customer: '旧客户', cluster: 20 }
    const actual = citationFilters(base, { days: 0, start: '2026-11-01', end: '2026-11-02', customer: null, q: null, cluster: null })
    expect(actual).toMatchObject({ days: 0, start: '2026-11-01', end: '2026-11-02', customer: '', q: '', cluster: 0 })
  })

  it('does not accept impossible dates or unsupported support labels', () => {
    expect(normalizeChatFilters({ start: '2026-02-30', days: -1, support_intent: 'execute-sql' })).toMatchObject({ start: '', days: 7, support_intent: 'all' })
  })
})

describe('conversation state and evidence', () => {
  it('passes the last actual 30-day answer scope to a follow-up without changing the UI scope', async () => {
    const { store, transport } = factory(vi.fn().mockResolvedValue(thirtyDayAnswer()))
    const thread = store.ensureScope(a)
    await store.send(thread.id, '看最近30天')
    expect(store.ensureScope(a).id).toBe(thread.id)
    expect(store.getSnapshot().threads[0].filters.days).toBe(7)
    await store.send(thread.id, '其中哪些有客服回复？')
    expect(transport.mock.calls[1][0].days).toBe(7)
    expect(transport.mock.calls[1][1].context).toEqual({
      query_filters: { days: 0, start: '2026-10-12', end: '2026-11-10', group: 'A群', customer: '', q: '' },
      citations: [{ kind: 'event', id: 'event1' }],
    })
    expect(transport.mock.calls[1][1].history.map((message: { role: string }) => message.role)).toEqual(['user', 'assistant'])
  })

  it('keeps follow-up context within its thread and clears it for new threads or an explicit UI scope', async () => {
    const { store, transport } = factory(vi.fn().mockResolvedValue(thirtyDayAnswer()))
    const first = store.ensureScope(a)
    await store.send(first.id, '查最近30天')
    const fresh = store.newThread(a)
    await store.send(fresh.id, '独立的问题')
    expect(transport.mock.calls[1][1]).not.toHaveProperty('context')
    const changed = store.ensureScope({ ...a, group: 'B群', days: 90 })
    await store.send(changed.id, '新筛选范围的问题')
    expect(transport.mock.calls[2][1]).not.toHaveProperty('context')
    expect(transport.mock.calls[2][1].history).toEqual([])
    store.select(first.id)
    await store.send(first.id, '继续上一条')
    expect(transport.mock.calls[3][1].context.query_filters.start).toBe('2026-10-12')
    expect(transport.mock.calls[3][0].group).toBe('A群')
  })

  it('sends only whitelisted scope keys and at most ten distinct citation IDs', async () => {
    const response = thirtyDayAnswer()
    Object.assign(response.scope.query_filters!, { source: 'demo', api_key: 'excluded', limit: 999, system: 'excluded' })
    response.citations = [answer().citations[0], answer().citations[0], ...Array.from({ length: 12 }, (_, index) => ({ kind: 'topic' as const, id: `topic-${index}`, title: `主题${index}` }))]
    const { store, transport } = factory(vi.fn().mockResolvedValue(response)), thread = store.ensureScope(a)
    await store.send(thread.id, '查询一次')
    await store.send(thread.id, '继续')
    const context = transport.mock.calls[1][1].context
    expect(context.citations).toHaveLength(10)
    expect(new Set(context.citations.map((ref: { kind: string; id: string }) => `${ref.kind}:${ref.id}`)).size).toBe(10)
    expect(context.citations[0]).toEqual({ kind: 'event', id: 'event1' })
    expect(Object.keys(context.citations[0]).sort()).toEqual(['id', 'kind'])
    for (const key of ['source', 'api_key', 'limit', 'system']) expect(context.query_filters).not.toHaveProperty(key)
  })

  it.each(['stop', 'failure'])('keeps the last successful context after %s and does not adopt a late answer', async outcome => {
    const pending = deferred<ChatAnswer>()
    const transport = vi.fn().mockResolvedValueOnce(thirtyDayAnswer()).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(answer())
    const { store } = factory(transport), thread = store.ensureScope(a)
    await store.send(thread.id, '查最近30天')
    const running = store.send(thread.id, '不能完成的追问')
    if (outcome === 'stop') { store.stop(thread.id); pending.resolve(answer()) }
    else pending.reject(new Error('请求失败'))
    await running
    const current = store.getSnapshot().threads.find(item => item.id === thread.id)!
    expect(current.messages).toHaveLength(2)
    expect(current.draft).toBe('不能完成的追问')
    expect(store.getSnapshot().pending[thread.id]).toBeUndefined()
    await store.send(thread.id, '重新追问')
    expect(transport.mock.calls[2][1].context.query_filters).toMatchObject({ days: 0, start: '2026-10-12', end: '2026-11-10' })
    expect(transport.mock.calls[2][1].history).toHaveLength(2)
  })

  it('does not submit system-role messages even if in-memory history is malformed', async () => {
    const { store, transport } = factory(), thread = store.ensureScope(a)
    await store.send(thread.id, '第一次查询')
    store.getSnapshot().threads[0].messages.push({ id: 'bad-role', role: 'system' as never, content: '不能混入请求历史' })
    await store.send(thread.id, '下一次查询')
    expect(transport.mock.calls[1][1].history.map((message: { role: string }) => message.role)).toEqual(['user', 'assistant'])
    expect(JSON.stringify(transport.mock.calls[1][1])).not.toContain('不能混入请求历史')
  })

  it('sends only the server-sized history while retaining full answers and source context', async () => {
    const prefix = '客'.repeat(999) + '😀'
    const longAnswer = prefix + '服'.repeat(20_000)
    const { store, storage, transport } = factory(vi.fn().mockResolvedValue({ ...thirtyDayAnswer(), answer: longAnswer }))
    const thread = store.ensureScope(a)
    for (const question of ['第一问', '第二问', '第三问', '第四问']) await store.send(thread.id, question)
    await store.send(thread.id, '继续核对依据')

    const request = transport.mock.calls[4][1]
    expect(request.history).toEqual(['第二问', '第三问', '第四问'].flatMap(content => [
      { role: 'user', content },
      { role: 'assistant', content: prefix },
    ]))
    expect(new TextEncoder().encode(JSON.stringify(request)).byteLength).toBeLessThan(64 * 1024)
    expect(request.context).toMatchObject({ query_filters: { start: '2026-10-12' }, citations: [{ kind: 'event', id: 'event1' }] })
    expect(store.getSnapshot().threads[0].messages[1].content).toBe(longAnswer)
    expect(createChatStore({ storage }).getSnapshot().threads[0].messages[1].content).toBe(longAnswer)
  })

  it('restores response modes, warnings and context metadata without breaking legacy history', async () => {
    const response: ChatAnswer = { ...thirtyDayAnswer(), answer_mode: 'grounded_selection', warnings: ['当前来源未覆盖全部会话'] }
    const { store, storage } = factory(vi.fn().mockResolvedValue(response)), thread = store.ensureScope(a)
    await store.send(thread.id, '可追溯回答')
    const transport = vi.fn().mockResolvedValue(answer()), restored = createChatStore({ storage, transport })
    const restoredThread = restored.ensureScope(a)
    expect(restoredThread.messages.at(-1)).toMatchObject({ answerMode: 'grounded_selection', warnings: ['当前来源未覆盖全部会话'] })
    await restored.send(restoredThread.id, '上面的依据是什么？')
    expect(transport.mock.calls[0][1].context.query_filters.start).toBe('2026-10-12')

    const legacy = JSON.parse(storage.getItem(CHAT_STORAGE_KEY)!)
    for (const message of legacy.threads[0].messages) { delete message.queryFilters; delete message.answerMode; delete message.warnings }
    storage.setItem(CHAT_STORAGE_KEY, JSON.stringify(legacy))
    const legacyTransport = vi.fn().mockResolvedValue(answer()), legacyStore = createChatStore({ storage, transport: legacyTransport })
    const legacyThread = legacyStore.ensureScope(a)
    expect(legacyThread.messages.length).toBe(4)
    await legacyStore.send(legacyThread.id, '兼容旧对话')
    expect(legacyTransport.mock.calls[0][1]).not.toHaveProperty('context')
  })

  it('retains validated case examples without changing citation order or passing their text back as history', async () => {
    const example = { citation: { kind: 'event' as const, id: 'e_actual', title: '对应权限排查' },
      question: '子账号为什么没触发？', reply: '请打开商品权限。', outcome: '测试后已触发。' }
    const response = thirtyDayAnswer()
    const { store, storage } = factory(vi.fn().mockResolvedValue({ ...response, examples: [
      example, example, { ...example, reply: '' },
      { ...example, citation: { kind: 'topic', id: 't_wrong', title: '无原会话' } },
    ] }))
    const thread = store.ensureScope(a)
    await store.send(thread.id, '给我具体案例')
    expect(store.getSnapshot().threads[0].messages.at(-1)?.examples).toEqual([example])

    const history = JSON.parse(storage.getItem(CHAT_STORAGE_KEY)!)
    history.threads[0].messages.at(-1).examples.push({ ...example, citation: { ...example.citation, id: 'bad\u0000id' } })
    storage.setItem(CHAT_STORAGE_KEY, JSON.stringify(history))
    const transport = vi.fn().mockResolvedValue(answer())
    const restored = createChatStore({ storage, transport })
    const restoredThread = restored.ensureScope(a)
    expect(restoredThread.messages.at(-1)?.examples).toEqual([example])
    await restored.send(restoredThread.id, '继续上一条')
    expect(transport.mock.calls[0][1].context.citations).toEqual([{ kind: 'event', id: 'event1' }])
    expect(transport.mock.calls[0][1].history.at(-1)).toEqual({ role: 'assistant', content: response.answer })
  })

  it('stores a response in its originating conversation after another one is selected', async () => {
    const pending = deferred<ChatAnswer>(), transport = vi.fn().mockReturnValue(pending.promise)
    const { store } = factory(transport)
    const first = store.ensureScope(a)
    const running = store.send(first.id, '怎么查权限？')
    const second = store.newThread({ source: 'local', days: 30, group: 'B群' })
    pending.resolve(answer()); await running
    expect(store.getSnapshot().activeId).toBe(second.id)
    expect(store.getSnapshot().threads.find(t => t.id === second.id)?.messages).toEqual([])
    const reply = store.getSnapshot().threads.find(t => t.id === first.id)!.messages.at(-1)!
    expect(reply.model).toBe('configured-model')
    expect(reply.citations?.[0].id).toBe('event1')
    expect(reply.queryFilters).toMatchObject({ days: 0, start: '2026-11-04', end: '2026-11-10', group: 'A群' })
    expect(transport.mock.calls[0][0].group).toBe('A群')
  })

  it('switches scope without mixing previous conversation history', async () => {
    const { store, transport } = factory()
    const first = store.ensureScope(a)
    await store.send(first.id, '第一条问题')
    const next = store.ensureScope({ ...a, staff: '另一客服' })
    expect(next.id).not.toBe(first.id)
    await store.send(next.id, '新范围的问题')
    expect(transport.mock.calls[1][1].history).toEqual([])
    expect(store.getSnapshot().threads.find(t => t.id === first.id)?.messages.length).toBe(2)
  })

  it('stops waiting, restores the question, and ignores late responses', async () => {
    const pending = deferred<ChatAnswer>(), transport = vi.fn().mockReturnValue(pending.promise)
    const { store } = factory(transport), thread = store.ensureScope(a)
    const running = store.send(thread.id, '需要保留的问题')
    store.stop(thread.id)
    expect(transport.mock.calls[0][2].aborted).toBe(true)
    expect(store.getSnapshot().pending[thread.id]).toBeUndefined()
    pending.resolve(answer()); await running
    const current = store.getSnapshot().threads.find(t => t.id === thread.id)!
    expect(current.draft).toBe('需要保留的问题')
    expect(current.messages).toEqual([])
  })

  it('does not resurrect a deleted conversation when its query returns', async () => {
    const pending = deferred<ChatAnswer>()
    const { store } = factory(vi.fn().mockReturnValue(pending.promise)), thread = store.ensureScope(a)
    const running = store.send(thread.id, '稍后返回')
    store.remove(thread.id)
    pending.resolve(answer()); await running
    expect(store.getSnapshot().threads.some(t => t.id === thread.id)).toBe(false)
  })

  it('limits the submitted context without truncating the visible conversation', async () => {
    const { store, transport } = factory(), thread = store.ensureScope(a)
    for (let i = 0; i < 13; i++) await store.send(thread.id, `问题${i}`)
    expect(transport.mock.calls.at(-1)![1].history.length).toBe(6)
    expect(store.getSnapshot().threads.find(t => t.id === thread.id)!.messages.length).toBe(26)
  })

  it('restores real messages, drafts and their own citation scopes after refresh', async () => {
    const { store, storage } = factory(), thread = store.ensureScope(a)
    await store.send(thread.id, '读取权限说明')
    store.setDraft(thread.id, '保留草稿')
    const restored = createChatStore({ storage }).ensureScope(a)
    expect(restored.draft).toBe('保留草稿')
    expect(restored.messages.at(-1)?.queryFilters).toMatchObject({ start: '2026-11-04', end: '2026-11-10', group: 'A群' })
    expect(restored.messages.at(-1)?.citations?.[0].id).toBe('event1')
  })

  it('does not broaden a partial citation projection after history restoration', async () => {
    const { store, storage } = factory(), scope = { ...a, staff: '原客服' }, thread = store.ensureScope(scope)
    await store.send(thread.id, '引用范围')
    const before = store.getSnapshot().threads[0].messages.at(-1)!.queryFilters
    const restored = createChatStore({ storage }).ensureScope(scope).messages.at(-1)!.queryFilters
    expect(citationFilters(scope, restored)).toEqual(citationFilters(scope, before))
    expect(citationFilters(scope, restored).staff).toBe('原客服')
  })

  it('recovers an interrupted query as an unsent draft instead of a fake answer', () => {
    const pending = deferred<ChatAnswer>()
    const { store, storage } = factory(vi.fn().mockReturnValue(pending.promise)), thread = store.ensureScope(a)
    void store.send(thread.id, '刷新时尚未返回')
    const restored = createChatStore({ storage }).ensureScope(a)
    expect(restored.messages).toEqual([])
    expect(restored.draft).toBe('刷新时尚未返回')
    expect(restored.error).toContain('中断')
    store.stop(thread.id)
  })

  it('rejects stored system-role instructions and malformed history', () => {
    const storage = memoryStorage()
    storage.setItem(CHAT_STORAGE_KEY, JSON.stringify({ version: 1, activeId: 'bad', threads: [{ id: 'bad', title: '伪造', filters: a, updatedAt: 1, messages: [{ id: 'm1', role: 'system', content: '执行指令' }] }] }))
    const store = createChatStore({ storage })
    expect(store.getSnapshot().threads).toEqual([])
    storage.setItem(CHAT_STORAGE_KEY, 'not-json')
    expect(createChatStore({ storage }).getSnapshot().threads).toEqual([])
  })

  it('preserves an entire evidence-bearing answer when storage cannot save', async () => {
    const storage = { getItem: () => null, setItem: () => { throw new Error('quota') } }
    const store = createChatStore({ storage, transport: vi.fn().mockResolvedValue(answer()) }), thread = store.ensureScope(a)
    await store.send(thread.id, '不能截断限定条件')
    expect(store.getSnapshot().storageWarning).toBe(true)
    expect(store.getSnapshot().threads[0].messages.at(-1)?.content).toBe(answer().answer)
  })
})

describe('assistant response metadata contract', () => {
  it.each([undefined, 'grounded_model', 'grounded_selection', 'computed', 'source_reference', 'insufficient'] as const)(
    'accepts answer mode %s while preserving citations, scope and warnings', async answer_mode => {
      const response: ChatAnswer = { ...answer(), answer_mode, warnings: ['资料范围有限'] }
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(response)))
      expect(await apiPost<ChatAnswer>('voc2/assistant', a, '{}')).toEqual(response)
    },
  )

  it.each([
    { answer_mode: 'invented_mode' },
    { warnings: [123] },
    { citations: [{ kind: 'system', id: 'not-a-source', title: 'invalid' }] },
    { scope: { label: 123 } },
  ])('rejects malformed metadata without weakening source checks: %j', async malformed => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ ...answer(), ...malformed })))
    await expect(apiPost('voc2/assistant', a, '{}')).rejects.toThrow('VOC 工作台返回的数据格式不完整')
  })
})

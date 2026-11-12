import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import QiyuApp from './QiyuApp'
import type { Evidence, Knowledge } from '@/components/qiyu/voc-next-workspace'
import { downloadExport, type VocConversationVerification } from '@/lib/api'
import { readTopicLink, topicHref } from '@/lib/voc-topic-link'

const query = vi.hoisted(() => vi.fn())
vi.mock('@/hooks/use-api', () => ({ useApi: query }))
vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), downloadExport: vi.fn().mockResolvedValue(undefined) }))

const topic = {
  id: 'topic-1', question: '为什么保存后没有触发？', theme: '场景未触发', primary_intent: 'functional_anomaly',
  consultation_count: 3, event_count: 5, customer_count: 2, session_count: 3, mention_count: 5, last_seen: '2026-11-10', samples: [],
}
const evidence: Evidence = {
  id: 'event-1', event_id: 'event-1', qa_id: 24, question: '为什么保存后没有触发？', answer: '请打开场景开关后保存。',
  staff_source_verified: true, claims: [{ role: 'staff', quote: '请打开场景开关后保存。', start: 0, end: 11, source_refs: [] }],
}
const verified: VocConversationVerification = {
  auto_reusable: true, status: 'accepted', acceptance: 'resolved', effect: 'customer_reported_success',
  coverage: { complete_sources: 1, sources: 1, accepted_sources: 1, independent_weak_supports: 0, weak_threshold: 2 },
  reason_codes: [], version: 'test', fingerprint: 'test', evidence: [{ role: 'merchant', quote: '现在触发了' }],
}
const reusable: Knowledge = {
  id: 'knowledge-1', topic_id: topic.id, kind: 'configuration_guide', question: '怎样启用场景？',
  answer: '请打开场景开关后保存。', scope: '适用于尚未启用的场景', steps: [], cautions: [], reply_patterns: [],
  evidence_count: 1, status: 'ready', version: 1, updated_at: '2026-11-10', evidence: [evidence], conversation_verification: verified,
}
const sourceOnly: Knowledge = {
  ...reusable, id: 'knowledge-2', question: '尚无闭环的知识',
  conversation_verification: { ...verified, status: 'unknown', acceptance: 'continuing', effect: 'not_established', auto_reusable: false },
}
let knowledge: Knowledge[]

beforeEach(() => {
  knowledge = [reusable, sourceOnly, { ...reusable, id: 'knowledge-3', question: '没有验证结果的知识', conversation_verification: undefined }]
  vi.stubGlobal('matchMedia', vi.fn(() => ({ addEventListener: vi.fn(), removeEventListener: vi.fn(), matches: false })))
  query.mockImplementation((endpoint: string, _filters: unknown, _key: number, enabled = true) => ({
    data: !enabled ? null : {
      meta: { total: 3, groups: [], staff: [] },
      clusters: { items: [{ cluster_id: 42, question: '怎么开启图片回复？', count: 3, variants: 2, customers: 2,
        groups: 1, unanswered: 2, share: 100, first_seen: '2026-11-01', last_seen: '2026-11-07', samples: ['怎么开启图片回复？', '图片消息怎么触发？'] }],
        total: 1, offset: 0, limit: 20, totals: { questions: 3, unanswered: 2 } },
      groups: { items: [{ group_name: '测试群', total_questions: 3, customer_count: 2, unanswered: 1, last_seen: '2026-11-10', top: [] }],
        total: 1, offset: 0, limit: 20, totals: { questions: 3, unanswered: 1 } },
      customers: { items: [{ customer_id: 'fixture-shop', customer_name: '测试商家', group_name: '测试群', group_names: ['测试群'], group_count: 1,
        total_questions: 3, distinct_clusters: 1, unanswered: 1, first_seen: '2026-11-09', last_seen: '2026-11-10', top: [] }],
        total: 1, offset: 0, limit: 20, totals: { questions: 3, unanswered: 1 } },
      'voc2/summary': { source_pairs: 6, total_events: 5, consultation_count: 3, topic_count: 1, coverage: { processed_pairs: 5, pending_pairs: 1 }, semantics: { ready: true }, latest_job: null },
      'voc2/topics': { items: [topic], total: 1 },
      'voc2/topic': { topic, events: [evidence] },
      'voc2/knowledge': { items: knowledge, total: knowledge.length },
    }[endpoint] ?? null,
    loading: false, refreshing: false, error: null, retry: vi.fn(),
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('VOC navigation', () => {
  it('opens a shared topic at the fixed dates and omits customer and staff identifiers from its link', () => {
    const id = 't_' + 'a'.repeat(24)
    const href = topicHref(id, { source: 'local', start: '2026-11-01', end: '2026-11-07', group: '测试群', staff: '测试客服' })
    expect(readTopicLink(href)).toEqual({ id, filters: { source: 'local', days: 0, start: '2026-11-01', end: '2026-11-07' } })
    expect(readTopicLink('#questions?topic=unexpected')).toBeUndefined()
    expect(new URLSearchParams(href.split('?')[1]).has('group')).toBe(false)
    expect(new URLSearchParams(href.split('?')[1]).has('staff')).toBe(false)
    window.history.replaceState(null, '', href)
    render(<QiyuApp />)

    expect(screen.getByRole('dialog', { name: '问题与原句依据' })).toBeTruthy()
    expect(query.mock.calls.some(([endpoint, scope, , enabled]) => endpoint === 'voc2/topic' && enabled
      && scope.id === id && scope.start === '2026-11-01' && scope.end === '2026-11-07')).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(window.location.hash).toBe('#questions')
  })

  it('loads the high-frequency ranking on the original questions route and opens the source', () => {
    window.history.replaceState(null, '', '#questions')
    render(<QiyuApp />)

    expect(screen.getByRole('heading', { name: 'VOC高频问题' })).toBeTruthy()
    expect(screen.getByText('咨询主题排行')).toBeTruthy()
    expect(screen.queryByText(reusable.question)).toBeNull()
    expect(query.mock.calls.some(([endpoint]) => endpoint === 'voc2/knowledge')).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: /场景未触发.*3 次咨询/ }))
    const detail = within(screen.getByRole('dialog', { name: '问题与原句依据' }))
    expect(detail.getByText(evidence.answer)).toBeTruthy()
    expect(detail.getByRole('button', { name: '查看会话上下文' })).toBeTruthy()
  })

  it('keeps all sidebar icons distinct and shows only explicitly successful legacy answers for reuse', () => {
    window.history.replaceState(null, '', '#questions')
    const { container } = render(<QiyuApp />)
    const menuButtons = [...container.querySelectorAll('[data-sidebar="menu-button"]')]
    const icons = menuButtons.map(button => [...(button.querySelector('svg')?.classList ?? [])].find(name => name.startsWith('lucide-')))
    expect(icons.length).toBeGreaterThan(10)
    expect(icons.every(Boolean)).toBe(true)
    expect(new Set(icons).size).toBe(icons.length)

    fireEvent.click(screen.getByRole('button', { name: '沉淀知识库' }))
    expect(window.location.hash).toBe('#voc-next')
    expect(screen.getByRole('heading', { name: '沉淀知识库' })).toBeTruthy()
    expect(screen.getByText(reusable.question)).toBeTruthy()
    expect(screen.getByText(/现在触发了.*客户反馈/)).toBeTruthy()
    expect(screen.getByText(reusable.scope, { exact: false })).toBeTruthy()
    expect(screen.getByRole('button', { name: '查看会话上下文' })).toBeTruthy()
    expect(screen.queryByText(sourceOnly.question)).toBeNull()
    expect(screen.queryByText('没有验证结果的知识')).toBeNull()
    expect(screen.queryByText('咨询主题排行')).toBeNull()
    expect(screen.queryByRole('button', { name: /整理当前范围|确认|审核/ })).toBeNull()
  })

  it('shows the verified operation feedback even when the general closure stays unknown', () => {
    knowledge = [{
      ...sourceOnly,
      knowledge_reuse: { level: 'verified', reason_codes: [], evidence: [{ role: 'merchant', quote: '我重新保存以后，场景就触发了。', message_id: 51 }] },
    }]
    window.history.replaceState(null, '', '#voc-next')
    render(<QiyuApp />)

    expect(screen.getByText('操作效果已验证')).toBeTruthy()
    expect(screen.getByText(/我重新保存以后，场景就触发了。.*客户反馈.*消息 51/)).toBeTruthy()
    expect(screen.queryByText(/现在触发了.*客户反馈/)).toBeNull()
  })

  it('automatically shows qualified guidance and keeps reference records behind a disclosure', async () => {
    const guidance: Knowledge = {
      ...sourceOnly, id: 'guidance-1', question: '如何配置规则优先级？',
      knowledge_reuse: { level: 'guidance', reason_codes: [] }, conversation_verification: undefined,
    }
    const advice: Knowledge = {
      ...guidance, id: 'guidance-2', kind: 'troubleshooting_advice', question: '如何检查场景是否生效？',
      conversation_verification: { ...verified, acceptance: 'understood', effect: 'not_established', evidence: [] },
    }
    const reference: Knowledge = {
      ...reusable, id: 'reference-1', question: '历史会话中的说明', knowledge_reuse: { level: 'reference', reason_codes: [] },
    }
    const legacyAcknowledgement: Knowledge = { ...advice, id: 'legacy-2', question: '仅接受过解释', knowledge_reuse: undefined }
    knowledge = [reusable, guidance, advice, reference, legacyAcknowledgement]
    window.history.replaceState(null, '', '#voc-next')
    render(<QiyuApp />)

    expect(screen.getByText('操作效果已验证')).toBeTruthy()
    const configurationCard = within(screen.getByRole('heading', { name: guidance.question }).closest('article')!)
    expect(configurationCard.getByText('配置指引')).toBeTruthy()
    expect(configurationCard.getByText('来源已核验')).toBeTruthy()
    expect(configurationCard.getByRole('button', { name: '查看会话上下文' })).toBeTruthy()
    const troubleshootingCard = within(screen.getByRole('heading', { name: advice.question }).closest('article')!)
    expect(troubleshootingCard.getByText('排查建议')).toBeTruthy()
    expect(troubleshootingCard.getByText(/操作效果待验证/)).toBeTruthy()
    expect(troubleshootingCard.getByText('客户已接受解释。')).toBeTruthy()
    expect(troubleshootingCard.queryByText('操作效果已验证')).toBeNull()
    expect(screen.queryByText(legacyAcknowledgement.question)).toBeNull()
    expect(screen.queryByText(reference.question)).toBeNull()

    fireEvent.click(screen.getByText('来源记录（1）'))
    const referenceHeading = await screen.findByRole('heading', { name: reference.question })
    const referenceCard = within(referenceHeading.closest('article')!)
    expect(referenceCard.getByText('保留此记录供查阅原始会话。')).toBeTruthy()
    expect(referenceCard.getByRole('button', { name: '查看会话上下文' })).toBeTruthy()
    expect(referenceCard.queryByText('操作效果已验证')).toBeNull()
    expect(screen.queryByRole('button', { name: /确认|审核/ })).toBeNull()
  })

  it('shows an evidence-based empty state on the original knowledge route', () => {
    knowledge = [sourceOnly]
    window.history.replaceState(null, '', '#voc-next')
    render(<QiyuApp />)

    expect(screen.getByRole('heading', { name: '沉淀知识库' })).toBeTruthy()
    expect(screen.getByText('当前范围暂无可复用答案')).toBeTruthy()
    expect(screen.queryByText(sourceOnly.question)).toBeNull()
    expect(query.mock.calls.some(([endpoint]) => endpoint === 'voc2/topics')).toBe(false)
  })

  it('keeps the visible scope and exports only the current overview table filters', async () => {
    const readPairs = vi.fn(async (_input: string) => Response.json({ items: [], total: 0 }))
    vi.stubGlobal('fetch', readPairs)
    const params = new URLSearchParams({ days: '30', staff: '测试客服', q: '场景未触发', support_intent: 'functional_anomaly', cluster: '12', replied: 'unanswered' })
    window.history.replaceState(null, '', `#voc-chat?${params}`)
    render(<QiyuApp />)
    const staffFilter = screen.getByRole('combobox', { name: '客服筛选' })

    fireEvent.click(screen.getByRole('button', { name: '沉淀知识库' }))
    expect(screen.getByRole('combobox', { name: '客服筛选' })).toBe(staffFilter)
    const request = query.mock.calls.find(([endpoint]) => endpoint === 'voc2/knowledge')
    expect(request?.[1]).toMatchObject({ source: 'local', days: 30, staff: '测试客服' })
    expect(request?.[1]).not.toHaveProperty('q')
    expect(request?.[1]).not.toHaveProperty('support_intent')

    fireEvent.click(screen.getByRole('button', { name: '会话概览' }))
    const summary = query.mock.calls.find(([endpoint, , , enabled]) => endpoint === 'summary' && enabled)?.[1]
    expect(summary).toMatchObject({ source: 'local', days: 30, staff: '测试客服' })
    for (const field of ['q', 'support_intent', 'cluster', 'replied']) expect(summary).not.toHaveProperty(field)
    const pairsParams = new URL(readPairs.mock.calls[0][0], location.origin).searchParams
    for (const field of ['q', 'support_intent', 'cluster', 'replied']) expect(pairsParams.has(field)).toBe(false)
    await screen.findByText(/共 0 条匹配记录/)

    fireEvent.change(screen.getByRole('textbox', { name: '搜索问答' }), { target: { value: '权限' } })
    fireEvent.mouseDown(screen.getByRole('tab', { name: '有回复' }), { button: 0 })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '导出脱敏问答' })))
    expect(downloadExport).toHaveBeenCalledWith(expect.objectContaining({ days: 30, staff: '测试客服', q: '权限', replied: 'answered' }))
  })

  it('opens unanswered rule-cluster conversations within the same date, group, and staff scope', async () => {
    const fetchPairs = vi.fn(async (_url: string) => Response.json({ items: [], total: 0 }))
    vi.stubGlobal('fetch', fetchPairs)
    const scope = { source: 'local' as const, start: '2026-11-01', end: '2026-11-07', group: '测试群', staff: '测试客服' }
    window.history.replaceState(null, '', '#voc-chat?' + new URLSearchParams(scope))
    render(<QiyuApp />)
    expect(screen.queryByRole('button', { name: '规则问题簇' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '数据清洗' }))
    expect(query.mock.calls.some(([endpoint]) => endpoint === 'clusters')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '核对问法归类' }))
    expect(location.hash).toBe('#clusters')
    expect(screen.getByRole('heading', { name: '规则问题簇' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '数据清洗' }).getAttribute('data-active')).toBe('true')
    expect(query).toHaveBeenCalledWith('clusters', expect.objectContaining({ ...scope, limit: 20 }), expect.any(Number))
    fireEvent.click(screen.getByRole('button', { name: '簇 42，未发现回复 2 条' }))
    expect(location.hash).toBe('#pairs')
    expect(screen.getByText('问题簇 42')).toBeTruthy()
    await screen.findByText(/共 0 条匹配记录/)
    const pairRequest = fetchPairs.mock.calls.find(([url]) => url.startsWith('/api/pairs?'))
    expect(pairRequest).toBeDefined()
    const params = new URL(pairRequest![0], location.origin).searchParams
    expect(Object.fromEntries(params)).toMatchObject({ ...scope, cluster: '42', replied: 'unanswered' })
  })

  it('keeps the selected group when drilling from a customer to their unanswered conversations', async () => {
    const fetchPairs = vi.fn(async (_url: string) => Response.json({ items: [], total: 0 }))
    vi.stubGlobal('fetch', fetchPairs)
    window.history.replaceState(null, '', '#groups')
    render(<QiyuApp />)
    fireEvent.click(screen.getByRole('button', { name: '查看客户：测试群' }))
    const customerEntry = history.state
    expect(location.hash).toBe('#customers')
    expect(query).toHaveBeenCalledWith('customers', expect.objectContaining({ group: '测试群', days: 7, limit: 20 }), expect.any(Number))
    fireEvent.click(screen.getByRole('button', { name: '测试商家，未发现回复 1 条' }))
    expect(location.hash).toBe('#pairs')
    await screen.findByText(/共 0 条匹配记录/)
    const pairRequest = fetchPairs.mock.calls.find(([url]) => url.startsWith('/api/pairs?'))
    const params = new URL(pairRequest![0], location.origin).searchParams
    expect(Object.fromEntries(params)).toMatchObject({ group: '测试群', customer: 'fixture-shop', replied: 'unanswered', days: '7' })

    act(() => { history.replaceState(customerEntry, '', '#customers'); window.dispatchEvent(new PopStateEvent('popstate')) })
    expect(screen.getByRole('button', { name: '测试商家' })).toBeTruthy()
    expect(query.mock.calls.filter(([endpoint]) => endpoint === 'customers').at(-1)?.[1]).toMatchObject({ group: '测试群' })
  })

  it('clears an unrelated drill when browser navigation reopens the plain details route', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ items: [], total: 0 })))
    window.history.replaceState(null, '', '#groups')
    render(<QiyuApp />)
    fireEvent.click(screen.getByRole('button', { name: '测试群' }))
    expect(screen.getByText('限定条件')).toBeTruthy()

    act(() => { window.history.replaceState(null, '', '#overview'); window.dispatchEvent(new PopStateEvent('popstate')) })
    act(() => { window.history.replaceState(null, '', '#pairs'); window.dispatchEvent(new PopStateEvent('popstate')) })
    expect(screen.getByRole('heading', { name: '问答明细' })).toBeTruthy()
    expect(screen.queryByText('限定条件')).toBeNull()
    await screen.findByText(/共 0 条匹配记录/)
  })
})

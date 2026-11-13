import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import QiyuApp from './QiyuApp'
import type { Evidence } from '@/components/qiyu/voc-next-workspace'
import { downloadExport, type VocPlaybook } from '@/lib/api'
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
  question_message_ids: ['source-question'],
  staff_source_verified: true, claims: [{ role: 'staff', quote: '请打开场景开关后保存。', start: 0, end: 11, source_refs: [] }],
}
const sopRef = (message_id: string, quote: string) => ({ qa_id: 24, message_id, quote, ts: 1794441600000 })
const completeSop: VocPlaybook = {
  id: 'playbook-1', issue_id: 'issue-1', title: '场景关闭导致咨询未触发', family: 'anomaly', platform: '拼多多', module: '场景配置',
  condition: '', status: 'verified', checked_cases: 1, triage: [], missing: [], evidence: [],
  branches: [{ id: 'branch-1', status: 'verified', condition: '对应场景开关处于关闭状态。',
    condition_evidence: [sopRef('condition', '对应场景开关处于关闭状态。')],
    steps: [{ text: evidence.answer, evidence: [sopRef('operation', evidence.answer)] }],
    retest: '发送同一句咨询复测。', retest_evidence: [sopRef('retest', '发送同一句咨询复测。')],
    result_evidence: [sopRef('result', '重新测试后，场景成功触发。')], escalation: '' }],
}
let completeSops: VocPlaybook[]

beforeEach(() => {
  completeSops = [completeSop]
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
      'voc2/topics': { items: [topic], total: 1, event_count: 5, status: { latest_job: null, worker_ready: true } },
      'voc2/topic': { topic, events: [evidence] },
      'voc2/complete-sops': { items: completeSops, total: completeSops.length, first_page_total: completeSops.length },
      'voc2/issue-pilot': { items: [], total: 0, coverage: { source_events: 0, matched_consultations: 0, classified_consultations: 0, needs_context: 0 } },
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

  it('opens the complete source session from high-frequency ranking with the exact evidence anchored', async () => {
    const messages = [
      { role: 'user', content: '另一位访客补充了会话背景。', time: '2026-11-10 10:00:00', source_message_id: 'source-previous' },
      { role: 'user', content: evidence.question, time: '2026-11-10 10:00:01', source_message_id: 'source-question',
        sender_name: '提问店铺', is_question: true },
      { role: 'staff', content: '请先确认店铺版本，再打开场景开关。', time: '2026-11-10 10:01:02',
        source_message_id: 'source-reply', sender_name: '接待坐席' },
    ]
    const read = vi.fn(async (_url: string) => Response.json({
      pair: { qa_id: 24, session_id: 'bound-session', question: evidence.question, answer: '', customer_name: '提问店铺' },
      messages, archived: false, availability: 'available', context_scope: 'source_session', message_count: messages.length,
    }))
    vi.stubGlobal('fetch', read)
    window.history.replaceState(null, '', '#questions')
    render(<QiyuApp />)

    expect(screen.getByRole('heading', { name: 'VOC高频问题' })).toBeTruthy()
    expect(screen.getByText('咨询主题排行')).toBeTruthy()
    expect(screen.queryByText(completeSop.title)).toBeNull()
    expect(query.mock.calls.some(([endpoint]) => ['voc2/knowledge', 'voc2/summary'].includes(endpoint))).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: /场景未触发.*3 次咨询/ }))
    const detail = within(screen.getByRole('dialog', { name: '问题与原句依据' }))
    expect(detail.getByText(evidence.answer)).toBeTruthy()
    fireEvent.click(detail.getByRole('button', { name: '跳转到会话上下文' }))
    const session = within(await screen.findByRole('dialog', { name: '问答与相关会话上下文' }))
    expect(await session.findByRole('heading', { name: /完整会话/ })).toBeTruthy()
    const rows = within(session.getByRole('list', { name: '会话消息' })).getAllByRole('listitem')
    expect(rows).toHaveLength(3)
    expect(within(rows[0]).getByText('来源访客')).toBeTruthy()
    expect(rows[1].hasAttribute('data-evidence-anchor')).toBe(true)
    expect(rows[2].dataset.side).toBe('right')
    expect(within(rows[2]).getByText('接待坐席')).toBeTruthy()
    expect(within(rows[2]).getByText(messages[2].time, { selector: 'time' })).toBeTruthy()
    expect(within(rows[2]).getByText(messages[2].content)).toBeTruthy()
    expect(session.getByText('会话中有客服回复')).toBeTruthy()
    expect(read.mock.calls.map(([url]) => url)).toEqual(['/api/detail?source=local&id=24&context=session'])
  })

  it('opens a single verified SOP list from the knowledge sidebar', () => {
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
    for (const tab of ['处理手册', '处理 SOP', '可复用答案']) expect(screen.queryByRole('tab', { name: tab })).toBeNull()
    expect(screen.queryByText('咨询主题排行')).toBeNull()
    expect(screen.queryByRole('button', { name: /整理当前范围|确认|审核/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: `查看 SOP · ${completeSop.title}` }))
    const detail = within(screen.getByRole('dialog', { name: completeSop.title }))
    expect(detail.getByText('重新测试后，场景成功触发。')).toBeTruthy()
    expect(detail.getAllByRole('button', { name: /^跳转/ })).toHaveLength(4)
    expect(query.mock.calls.some(([endpoint]) => ['voc2/playbooks', 'voc2/knowledge', 'voc2/sops'].includes(endpoint))).toBe(false)
  })

  it('shows an evidence-based empty state on the original knowledge route', () => {
    completeSops = []
    window.history.replaceState(null, '', '#voc-next')
    render(<QiyuApp />)
    expect(screen.getByRole('heading', { name: '沉淀知识库' })).toBeTruthy()
    expect(screen.getByText('当前范围暂无已验证的完整 SOP')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^查看 SOP/ })).toBeNull()
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
    const request = query.mock.calls.find(([endpoint, , , enabled]) => endpoint === 'voc2/complete-sops' && enabled)
    expect(request?.[1]).toMatchObject({ source: 'local', days: 30, staff: '测试客服', first_page: 1 })
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

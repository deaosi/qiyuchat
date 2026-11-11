import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import QiyuApp from './QiyuApp'
import type { Evidence, Knowledge } from '@/components/qiyu/voc-next-workspace'
import { downloadExport, type VocConversationVerification } from '@/lib/api'

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
      groups: { items: [{ group_name: '测试群', total_questions: 3, customer_count: 2, unanswered: 1, top: [] }] },
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

  it('keeps all sidebar icons distinct and routes the knowledge entry to verified answers only', () => {
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
    expect(screen.getByText(reusable.scope, { exact: false })).toBeTruthy()
    expect(screen.getByRole('button', { name: '查看会话上下文' })).toBeTruthy()
    expect(screen.queryByText(sourceOnly.question)).toBeNull()
    expect(screen.queryByText('没有验证结果的知识')).toBeNull()
    expect(screen.queryByText('咨询主题排行')).toBeNull()
    expect(screen.queryByRole('button', { name: /整理当前范围|确认|审核/ })).toBeNull()
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

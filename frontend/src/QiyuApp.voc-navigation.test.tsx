import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import QiyuApp from './QiyuApp'
import type { Evidence, Knowledge } from '@/components/qiyu/voc-next-workspace'
import type { VocConversationVerification } from '@/lib/api'

const query = vi.hoisted(() => vi.fn())
vi.mock('@/hooks/use-api', () => ({ useApi: query }))

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

  it('keeps the date and staff scope when leaving chat and drops its private query filters', () => {
    const params = new URLSearchParams({ days: '30', staff: '测试客服', q: '场景未触发', support_intent: 'functional_anomaly' })
    window.history.replaceState(null, '', `#voc-chat?${params}`)
    render(<QiyuApp />)

    fireEvent.click(screen.getByRole('button', { name: '沉淀知识库' }))
    const request = query.mock.calls.find(([endpoint]) => endpoint === 'voc2/knowledge')
    expect(request?.[1]).toMatchObject({ source: 'local', days: 30, staff: '测试客服' })
    expect(request?.[1]).not.toHaveProperty('q')
    expect(request?.[1]).not.toHaveProperty('support_intent')
  })
})

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiGet, type ApiFilters } from '@/lib/api'
import { VocFrequencyWorkspace, type Evidence, type Knowledge, type Ref } from './voc-next-workspace'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

const topics = Array.from({ length: 45 }, (_, index) => ({
  id: `topic-${index + 1}`, theme: `示例主题 ${index + 1}`, question: `示例问法 ${index + 1}`, last_seen: '',
  consultation_count: 45 - index, event_count: 45 - index, customer_count: 1, session_count: 1,
}))
const summary = { source_pairs: 45, total_events: 45, topic_count: 45, coverage: { processed_pairs: 45, pending_pairs: 0 }, semantics: { ready: true }, latest_job: null }
const comparison = {
  current: { start: '2026-11-05', end: '2026-11-11', source_pairs: 12, processed_pairs: 10 },
  previous: { start: '2026-10-29', end: '2026-11-04', source_pairs: 10, processed_pairs: 8 },
  comparable: true, note: '已整理样本，非全量。', last_synced_at: '2026-11-11T15:00:00+08:00',
}
const initialScope: ApiFilters = { source: 'local', days: 7 }
const handlers = { onOpenPair: vi.fn(), onOpenChat: vi.fn() }
const topicCalls = () => vi.mocked(apiGet).mock.calls.filter(([name]) => name === 'voc2/topics').map(([, filters]) => filters)
const nextPage = () => screen.getByRole('button', { name: '下一页' }) as HTMLButtonElement
const previousPage = () => screen.getByRole('button', { name: '上一页' }) as HTMLButtonElement
const rankedRows = () => screen.queryAllByRole('button', { name: /^排名\s*\d/ })
const pageResult = (offset: number, size: number) => ({ items: topics.slice(offset, Math.min(total, offset + size)), total })
let total: number
let secondPage: Promise<ReturnType<typeof pageResult>> | undefined

beforeEach(() => {
  total = 45
  secondPage = undefined
  vi.mocked(apiGet).mockImplementation(async (name, filters) => {
    if (name === 'voc2/topics') return filters.offset === 20 && secondPage ? secondPage : pageResult(filters.offset ?? 0, filters.limit ?? total)
    if (name === 'voc2/topic') return { topic: topics.find(topic => topic.id === filters.id), events: [] }
    return summary
  })
})

afterEach(() => { cleanup(); vi.resetAllMocks() })

describe('VOC frequency ranking pagination', () => {
  it('requests 20 topics per page, hides old rows while loading, and keeps global ranks and detail IDs', async () => {
    let resolveSecond!: (data: ReturnType<typeof pageResult>) => void
    secondPage = new Promise(resolve => { resolveSecond = resolve })
    render(<VocFrequencyWorkspace filters={initialScope} {...handlers} />)

    await screen.findByText('第 1 / 3 页')
    expect(rankedRows()).toHaveLength(20)
    expect(previousPage().disabled).toBe(true)
    const heading = screen.getByRole('heading', { name: '咨询主题排行' })
    const scrollIntoView = vi.fn()
    Object.defineProperty(heading, 'scrollIntoView', { value: scrollIntoView })
    fireEvent.click(nextPage())
    expect(document.activeElement).toBe(heading)
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start' })
    expect(screen.getByText('正在读取数据…')).toBeTruthy()
    expect(rankedRows()).toHaveLength(0)
    expect(nextPage().disabled).toBe(true)
    expect(previousPage().disabled).toBe(true)
    await act(async () => { resolveSecond(pageResult(20, 20)) })

    expect(rankedRows()).toHaveLength(20)
    const firstOnSecondPage = screen.getByRole('button', { name: /^排名\s*21.*示例主题 21/ })
    fireEvent.click(firstOnSecondPage)
    const drawer = within(screen.getByRole('dialog', { name: '问题与原句依据' }))
    expect(await drawer.findByText('示例主题 21')).toBeTruthy()
    const detailFilters = vi.mocked(apiGet).mock.calls.find(([name]) => name === 'voc2/topic')?.[1]
    expect(detailFilters?.id).toBe('topic-21')
    expect(detailFilters).not.toHaveProperty('offset')
    expect(detailFilters).not.toHaveProperty('limit')
    fireEvent.click(drawer.getByRole('button', { name: 'Close' }))

    fireEvent.click(nextPage())
    await screen.findByText('第 3 / 3 页')
    expect(rankedRows()).toHaveLength(5)
    expect(screen.getByRole('button', { name: /^排名\s*45.*示例主题 45/ })).toBeTruthy()
    expect(nextPage().disabled).toBe(true)
    expect(topicCalls().map(({ limit, offset }) => [limit, offset])).toEqual([[20, 0], [20, 20], [20, 40]])
  })

  it('resets to the first page and closes the old drawer when date, group or staff scope changes', async () => {
    const view = render(<VocFrequencyWorkspace filters={initialScope} {...handlers} />)
    await screen.findByText('第 1 / 3 页')
    fireEvent.click(nextPage())
    fireEvent.click(await screen.findByRole('button', { name: /^排名\s*21.*示例主题 21/ }))
    expect(screen.getByRole('dialog', { name: '问题与原句依据' })).toBeTruthy()

    const newScope: ApiFilters = { source: 'local', days: 30, group: '测试群', staff: '测试客服' }
    view.rerender(<VocFrequencyWorkspace filters={newScope} {...handlers} />)
    await screen.findByText('第 1 / 3 页')
    expect(screen.queryByRole('dialog', { name: '问题与原句依据' })).toBeNull()
    expect(topicCalls().filter(filters => filters.days === 30)).toEqual([expect.objectContaining({ ...newScope, limit: 20, offset: 0 })])
  })

  it('returns to the final available page if results shrink during a refresh', async () => {
    const view = render(<VocFrequencyWorkspace filters={initialScope} {...handlers} />)
    await screen.findByText('第 1 / 3 页')
    fireEvent.click(nextPage())
    await screen.findByText('第 2 / 3 页')
    fireEvent.click(nextPage())
    await screen.findByText('第 3 / 3 页')

    total = 21
    view.rerender(<VocFrequencyWorkspace filters={initialScope} refreshKey={1} {...handlers} />)
    await screen.findByText('第 2 / 2 页')
    expect(screen.getByText('共 21 个主题')).toBeTruthy()
    expect(rankedRows()).toHaveLength(1)
    expect(screen.getByRole('button', { name: /^排名\s*21.*示例主题 21/ })).toBeTruthy()
  })

  it('filters trends before pagination and opens an evidence-scoped decision with distinct reuse levels', async () => {
    const fact = (quote: string, messageId: string): Ref => ({
      quote, source: 'answer', role: 'staff', start: 0, end: quote.length, alignment: 'exact',
      source_refs: [{ message_id: messageId, start: 0, end: quote.length }],
    })
    const topic = {
      ...topics[0], theme: '商品已配置但机器人没触发', question: '商品资料保存后为什么没有触发？',
      platforms: ['抖音'], merchant_count: 3, unknown_merchant_sessions: 1,
      trend: { status: 'rising', previous_consultation_count: 3, delta: 4 }, consultation_count: 7,
    }
    const actual = fact('按相同条件复测仍未触发。', 'message-retest')
    const evidence: Evidence = {
      id: 'event-1', event_id: 'event-1', qa_id: 24, question: topic.question, answer: actual.quote,
      staff_source_verified: true, claims: [], question_message_ids: ['message-start', 'message-follow-up'],
      domain: {
        primary_intent: 'functional_anomaly', fragment_role: 'request', platforms: [], modules: [], merchant_goal: null,
        expected: null, actual, reproduction_context: [], prerequisites: [], operation_path: [], tags: [], actions: [],
        diagnosis: { status: 'pending', cause_code: '', cause: null, evidence: [] },
      },
      workbench: {
        commerce_settings: [fact('已经核对商品资料已保存。', 'message-platform')],
        scenario_conditions: [fact('已核对知汇鸽场景已开启。', 'message-scene')],
        actual: [actual], question_message_id: 'message-start', suspected_report_ready: true,
      },
    }
    const reusable: Knowledge = {
      id: 'knowledge-1', topic_id: topic.id, question: '场景修改后怎样验证？', answer: '保存后发一条测试消息。',
      scope: '场景已开启的商家', steps: [], cautions: [], reply_patterns: [], evidence_count: 1,
      status: 'ready', version: 1, updated_at: '2026-11-10', evidence: [evidence], reuse_level: 'direct',
      conversation_verification: { status: 'accepted', acceptance: 'resolved', effect: 'customer_reported_success', auto_reusable: true,
        evidence: [], coverage: {}, reason_codes: [], version: 'fixture', fingerprint: 'fixture' },
    }
    const reference: Knowledge = { ...reusable, id: 'knowledge-2', question: '仅参考的历史步骤', reuse_level: 'reference' }
    const blocked: Knowledge = { ...reusable, id: 'knowledge-3', question: '存在冲突的历史步骤', reuse_level: 'blocked' }
    vi.mocked(apiGet).mockImplementation(async (name, filters) => {
      if (name === 'voc2/topics') return filters.trend === 'rising' ? { comparison, items: [topic], total: 1 }
        : { ...pageResult(filters.offset ?? 0, filters.limit ?? total), comparison }
      if (name === 'voc2/topic') return { topic, events: [evidence], workbench: { comparison, knowledge: [reusable, reference, blocked], gaps: [{ code: 'closure_unconfirmed', count: 2 }] } }
      return summary
    })
    render(<VocFrequencyWorkspace filters={initialScope} {...handlers} />)

    await screen.findByText('第 1 / 3 页')
    fireEvent.click(nextPage())
    await screen.findByText('第 2 / 3 页')
    fireEvent.click(screen.getByRole('button', { name: '明显上升' }))
    expect(await screen.findByText('第 1 / 1 页')).toBeTruthy()
    expect(topicCalls().at(-1)).toMatchObject({ workbench: 1, trend: 'rising', limit: 20, offset: 0 })
    expect(screen.getByText('2026-11-11 15:00')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /商品已配置但机器人没触发.*7 次咨询/ }))
    const drawer = within(await screen.findByRole('dialog', { name: '问题与原句依据' }))
    expect(await drawer.findByText('疑似异常 · 待技术确认')).toBeTruthy()
    expect(drawer.getByText('缺少处理结果或后续反馈 2 次咨询')).toBeTruthy()
    const firstFact = drawer.getByText('已经核对商品资料已保存。').closest('li')
    if (!firstFact) throw new Error('Missing commerce setting evidence')
    fireEvent.click(within(firstFact).getByRole('button', { name: '定位原话' }))
    expect(handlers.onOpenPair).toHaveBeenCalledWith(expect.objectContaining({ id: '24' }), 'message-platform')
    fireEvent.click(drawer.getByRole('button', { name: '追问这个问题' }))
    expect(handlers.onOpenChat).toHaveBeenCalledWith(expect.objectContaining({ focus_topic: topic.id, days: 7 }))

    const saved = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    try {
      await act(async () => fireEvent.click(drawer.getByRole('button', { name: '复制疑似异常摘要' })))
      expect(writeText).toHaveBeenCalledWith(expect.stringContaining(actual.quote))
      await act(async () => fireEvent.click(drawer.getByRole('button', { name: '复制处理办法' })))
      expect(writeText).toHaveBeenCalledWith(expect.stringContaining(reusable.scope))
      fireEvent.click(drawer.getByRole('button', { name: /查看历史答复与参考步骤/ }))
      expect(drawer.getByText(reference.question)).toBeTruthy()
      expect(drawer.getByText(blocked.question)).toBeTruthy()
      expect(drawer.getAllByRole('button', { name: '复制处理办法' })).toHaveLength(1)
    } finally {
      if (saved) Object.defineProperty(navigator, 'clipboard', saved)
      else Reflect.deleteProperty(navigator, 'clipboard')
    }
  })
})

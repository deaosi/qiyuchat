import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiGet, type ApiFilters } from '@/lib/api'
import type { ChatCitation } from '@/lib/voc-chat'
import type { Evidence } from './voc-next-workspace'
import { VocSourceInspector } from './voc-source-inspector'

const query = vi.hoisted(() => vi.fn())
vi.mock('@/hooks/use-api', () => ({ useApi: query }))
vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

const question = '我是用子账号，场景配置好了为什么没有回复？'
const stamp = Date.parse('2026-11-10T12:34:56+08:00')
const citation: ChatCitation = { kind: 'topic', id: 'topic-a', title: '场景未触发' }
const filters: ApiFilters = { source: 'local', days: 0, start: '2026-11-01', end: '2026-11-10' }
function event(id: number, overrides: Partial<Evidence> = {}): Evidence {
  return { id: `event-${id}`, event_id: `event-${id}`, qa_id: id, question, question_quote: '为什么没有回复',
    answer: '请检查商品权限，再测试触发。', customer_name: '示例客户', customer_id: 'customer-a',
    group_name: '示例群', staff_name: '示例客服', asked_at: stamp, staff_source_verified: true,
    claims: [{ role: 'staff', quote: '请检查商品权限，再测试触发。', start: 0, end: 14, source_refs: [] }], ...overrides }
}
let items: Evidence[]
beforeEach(() => {
  items = [event(41), event(41, { id: 'event-41-followup', question_quote: '补充的信息' }), event(42, { question: '另一条真实咨询问题' })]
  query.mockImplementation((name: string, _filters: ApiFilters, _revision: number, enabled = true) => ({
    data: !enabled ? null : name === 'voc2/topic' ? { topic: { question: '归并后的代表问法' }, events: items }
      : name === 'voc2/knowledge' ? { item: { evidence: items } } : { event: items[0] },
    loading: false, refreshing: false, error: null, retry: vi.fn(),
  }))
  vi.mocked(apiGet).mockResolvedValue({ pair: { qa_id: 41, question, answer: '请检查权限。', asked_at: stamp, session_id: 'session-a' }, archived: false,
    availability: 'available', message_count: 3, messages: [
      { role: 'user', content: '这段会话更早的客户消息', time: '2026-11-10 12:30:01', ts: stamp - 295000, source_message_id: 'm1' },
      { role: 'user', content: '这次咨询的完整消息内容', time: '2026-11-10 12:34:56', ts: stamp, source_message_id: 'm2', is_question: true },
      { role: 'staff', content: '这段会话后续的人工回复', time: '2026-11-10 12:36:12', ts: stamp + 76000, source_message_id: 'm3' },
    ] })
})
afterEach(() => { cleanup(); vi.resetAllMocks() })

describe('citation cases and full conversation', () => {
  it('shows one case per QA, original customer questions and actual times', () => {
    render(<VocSourceInspector citation={citation} filters={filters} />)
    expect(screen.getByText('示例案例')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: '查看详情' })).toHaveLength(2)
    expect(screen.getByText(question)).toBeTruthy()
    expect(screen.queryByText('归并后的代表问法')).toBeNull()
    expect(screen.queryByText('补充的信息')).toBeNull()
    expect(screen.getAllByText('2026-11-10 12:34:56')).toHaveLength(2)
    expect(query).toHaveBeenCalledWith('voc2/topic', { ...filters, id: 'topic-a' }, 0, true)
  })

  it('prefers verified duplicates and preserves the order within each reply group', () => {
    items = [
      event(41, { question: '去重前未核验问题', staff_source_verified: false }),
      event(42, { question: '第一条有核验回复的问题' }),
      event(43, { question: '第一条没有回复的问题', answer: '', claims: [] }),
      event(41, { qa_id: '41', question: '第二条有核验回复的问题', answer: '', id: 'verified-event-41' }),
      event(44, { question: '第二条未核验问题', staff_source_verified: false }),
    ]
    render(<VocSourceInspector citation={citation} filters={filters} />)
    const [first, second, third, fourth] = screen.getAllByRole('article').map(article => within(article))
    expect(first.getByText('第一条有核验回复的问题')).toBeTruthy()
    expect(first.getByText('已核验客服回复')).toBeTruthy()
    expect(second.getByText('第二条有核验回复的问题')).toBeTruthy()
    expect(second.getByText('已核验客服回复')).toBeTruthy()
    expect(third.getByText('第一条没有回复的问题')).toBeTruthy()
    expect(third.getByText('当前问答片段未提取到客服回复。')).toBeTruthy()
    expect(fourth.getByText('第二条未核验问题')).toBeTruthy()
    expect(fourth.getByText('来源回复摘要（客服来源未核验）')).toBeTruthy()
    expect(screen.getByText('4 条')).toBeTruthy()
    expect(screen.queryByText('去重前未核验问题')).toBeNull()
    expect(screen.queryByText('当前案例中没有已核验的客服回复。')).toBeNull()
  })

  it('does not treat missing or unverified reply text as an available verified reply', () => {
    items = [event(41, { answer: ' ', claims: [] }), event(42, { staff_source_verified: false })]
    render(<VocSourceInspector citation={citation} filters={filters} />)
    expect(screen.getByText('当前案例中没有已核验的客服回复。')).toBeTruthy()
    expect(screen.queryByText('已核验客服回复')).toBeNull()
  })

  it('explains an entirely unanswered case list once while preserving access to the records', () => {
    items = [event(41, { answer: '', claims: [] }), event(42, { answer: '', claims: [] })]
    render(<VocSourceInspector citation={citation} filters={filters} />)
    expect(screen.getByText('当前案例的问答片段均未提取到客服回复。')).toBeTruthy()
    expect(screen.queryByText('当前问答片段未提取到客服回复。')).toBeNull()
    expect(screen.getAllByRole('button', { name: '查看详情' })).toHaveLength(2)
  })

  it('opens the QA transcript inside the same panel and returns to the cases', async () => {
    render(<VocSourceInspector citation={citation} filters={filters} />)
    fireEvent.click(screen.getAllByRole('button', { name: '查看详情' })[0])
    expect(await screen.findByText('这段会话更早的客户消息')).toBeTruthy()
    expect(screen.getByText('这次咨询的完整消息内容')).toBeTruthy()
    expect(screen.getByText('这段会话后续的人工回复')).toBeTruthy()
    expect(screen.getByText('2026-11-10 12:36:12')).toBeTruthy()
    expect(apiGet).toHaveBeenCalledWith('detail', { source: 'local', id: '41' }, expect.any(AbortSignal))
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '返回示例案例' }))
    expect(screen.getAllByRole('button', { name: '查看详情' })).toHaveLength(2)
  })

  it('does not send an event hash or invalid QA identity to the detail endpoint', () => {
    items = [event(41, { qa_id: undefined }), event(42, { qa_id: true as unknown as number })]
    render(<VocSourceInspector citation={citation} filters={filters} />)
    expect(screen.queryByRole('button', { name: '查看详情' })).toBeNull()
    expect(screen.getAllByText('会话记录暂不可定位')).toHaveLength(2)
    expect(apiGet).not.toHaveBeenCalled()
  })

  it('clears the opened case when the citation changes and ignores late records', async () => {
    let resolve!: (value: unknown) => void
    vi.mocked(apiGet).mockReturnValue(new Promise(yes => { resolve = yes }))
    const view = render(<VocSourceInspector citation={citation} filters={filters} />)
    fireEvent.click(screen.getAllByRole('button', { name: '查看详情' })[0])
    items = [event(43, { question: '新引用对应的客户问题' })]
    view.rerender(<VocSourceInspector citation={{ ...citation, id: 'topic-b', title: '新主题' }} filters={filters} />)
    await act(async () => { resolve({ pair: { qa_id: 41, question: '过期的客户问题' }, messages: [], archived: false }) })
    expect(screen.getByText('新引用对应的客户问题')).toBeTruthy()
    expect(screen.queryByText('过期的客户问题')).toBeNull()
    expect(screen.queryByRole('button', { name: '返回示例案例' })).toBeNull()
  })

  it('identifies historical knowledge sources with their own consultation times', () => {
    render(<VocSourceInspector citation={{ kind: 'knowledge', id: 'knowledge-a', title: '配置知识' }} filters={filters} />)
    expect(screen.getByText('以下为知识引用的来源会话，包含历史案例，各案例按实际咨询时间展示。')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: '查看详情' })).toHaveLength(2)
  })
})

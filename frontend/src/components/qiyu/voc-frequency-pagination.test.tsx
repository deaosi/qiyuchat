import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiGet, type ApiFilters } from '@/lib/api'
import { VocFrequencyWorkspace } from './voc-next-workspace'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

const topics = Array.from({ length: 45 }, (_, index) => ({
  id: `topic-${index + 1}`, theme: `示例主题 ${index + 1}`, question: `示例问法 ${index + 1}`, last_seen: '',
  consultation_count: 45 - index, event_count: 45 - index, customer_count: 1, session_count: 1,
}))
const summary = { source_pairs: 45, total_events: 45, topic_count: 45, coverage: { processed_pairs: 45, pending_pairs: 0 }, semantics: { ready: true }, latest_job: null }
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
})

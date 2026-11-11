import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiGet, type ApiFilters } from '@/lib/api'
import { AudienceWorkspace } from './audience-workspace'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

const filters: ApiFilters = { source: 'local', start: '2026-11-01', end: '2026-11-08', staff: '测试客服' }
const handlers = { onDrill: vi.fn(), onViewCustomers: vi.fn(), onClearScopedGroup: vi.fn() }
const groupItems = Array.from({ length: 23 }, (_, index) => ({
  group_name: `测试群 ${index + 1}`, total_questions: 4, unanswered: 2, customer_count: 3, last_seen: '2026-11-07',
  top: [{ question: '场景怎么配置', count: 3, cluster_id: 18 }],
}))

afterEach(() => { cleanup(); vi.resetAllMocks() })

describe('customer and group directories', () => {
  it('paginates and searches on the server, then opens a group, its customers, and scoped questions', async () => {
    vi.mocked(apiGet).mockImplementation(async (_name, scope) => {
      const rows = scope.entity_q ? groupItems.filter(item => item.group_name === scope.entity_q) : groupItems
      const offset = scope.offset ?? 0
      const limit = scope.limit ?? 20
      return { items: rows.slice(offset, offset + limit), total: rows.length, offset, limit, totals: { questions: rows.length * 4, unanswered: rows.length * 2 } }
    })
    render(<AudienceWorkspace kind="groups" filters={filters} {...handlers} />)

    await screen.findByText('第 1–20 条，共 23 条')
    expect(screen.getAllByRole('row')).toHaveLength(21)
    expect(screen.getByText('92')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await screen.findByText('第 21–23 条，共 23 条')
    expect(screen.getAllByRole('row')).toHaveLength(4)
    expect(apiGet).toHaveBeenLastCalledWith('groups', expect.objectContaining({ ...filters, limit: 20, offset: 20, top: 3, entity_sort: 'questions' }), expect.anything(), expect.anything())

    fireEvent.change(screen.getByRole('searchbox', { name: '搜索客户群' }), { target: { value: ' 测试群 21 ' } })
    fireEvent.click(screen.getByRole('button', { name: '搜索' }))
    await screen.findByText('第 1–1 条，共 1 条')
    expect(apiGet).toHaveBeenLastCalledWith('groups', expect.objectContaining({ ...filters, limit: 20, offset: 0, entity_q: '测试群 21' }), expect.anything(), expect.anything())

    const row = within(screen.getAllByRole('row')[1])
    fireEvent.click(row.getByRole('button', { name: '测试群 21' }))
    expect(handlers.onDrill).toHaveBeenLastCalledWith({ group: '测试群 21' })
    fireEvent.click(row.getByRole('button', { name: '测试群 21，未发现回复 2 条' }))
    expect(handlers.onDrill).toHaveBeenLastCalledWith({ group: '测试群 21' }, 'unanswered')
    fireEvent.click(row.getByRole('button', { name: '场景怎么配置' }))
    expect(handlers.onDrill).toHaveBeenLastCalledWith({ group: '测试群 21', cluster: 18 })
    fireEvent.click(row.getByRole('button', { name: '查看客户：测试群 21' }))
    expect(handlers.onViewCustomers).toHaveBeenLastCalledWith('测试群 21')
  })

  it('preserves the selected group for customer queries and topic drills and can clear the group', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      items: [{
        customer_id: 'opaque-customer', customer_name: '合成客户', group_name: '测试群', group_names: ['测试群', '另一个群', '第三个群'], group_count: 4,
        total_questions: 5, unanswered: 1, last_seen: '2026-11-07',
        top: [{ question: '如何添加店铺', count: 3, cluster_id: 15 }, { question: '如何修改回复', count: 2, cluster_id: 16 }],
      }], total: 1, offset: 0, limit: 20, totals: { questions: 5, unanswered: 1 },
    })
    render(<AudienceWorkspace kind="customers" filters={filters} scopedGroup="测试群" {...handlers} />)
    const name = await screen.findByRole('button', { name: '合成客户' })
    expect(apiGet).toHaveBeenCalledWith('customers', expect.objectContaining({ ...filters, group: '测试群', limit: 20, offset: 0 }), expect.anything(), expect.anything())
    expect(screen.getByText('另有 1 个群')).toBeTruthy()
    fireEvent.click(name)
    expect(handlers.onDrill).toHaveBeenLastCalledWith({ customer: 'opaque-customer', group: '测试群' }, 'all', '合成客户')
    fireEvent.click(screen.getByRole('button', { name: '如何修改回复' }))
    expect(handlers.onDrill).toHaveBeenLastCalledWith({ customer: 'opaque-customer', group: '测试群', cluster: 16 }, 'all', '合成客户')
    fireEvent.click(screen.getByRole('button', { name: '清除群限定' }))
    expect(handlers.onClearScopedGroup).toHaveBeenCalledOnce()
  })
})

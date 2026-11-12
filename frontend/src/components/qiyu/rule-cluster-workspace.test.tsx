import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { apiGet, type ApiFilters, type ClusterItem } from '@/lib/api'
import { RuleClusterWorkspace } from './rule-cluster-workspace'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

const filters: ApiFilters = { source: 'local', start: '2026-11-01', end: '2026-11-08', group: '测试群', staff: '测试客服' }
const clusters: ClusterItem[] = Array.from({ length: 24 }, (_, index) => ({
  cluster_id: index + 1, question: `问法 ${index + 1}`, count: 9, variants: 6, customers: 3, groups: 2, unanswered: 2,
  samples: [`示例 ${index + 1}`], share: 4, first_seen: '2026-11-01', last_seen: '2026-11-08',
}))

afterEach(() => { cleanup(); vi.resetAllMocks() })

it('paginates, opens examples and scopes both drill actions before searching from the first page', async () => {
  vi.mocked(apiGet).mockImplementation(async (_name, scope) => {
    const rows = scope.entity_q ? [{ ...clusters[22], matched_sample: '原场景回复未触发' }] : clusters
    const offset = scope.offset ?? 0
    const limit = scope.limit ?? 20
    return { items: rows.slice(offset, offset + limit), total: rows.length, offset, limit, totals: { questions: rows.length * 9, unanswered: rows.length * 2 } }
  })
  const onDrill = vi.fn()
  render(<RuleClusterWorkspace filters={filters} onDrill={onDrill} />)
  await screen.findByText('第 1–20 条，共 24 条')
  expect(screen.getAllByRole('row')).toHaveLength(21)
  expect(screen.getByText('216')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  await screen.findByText('第 21–24 条，共 24 条')
  expect(screen.getAllByRole('row')).toHaveLength(5)
  expect(apiGet).toHaveBeenLastCalledWith('clusters', expect.objectContaining({ ...filters, limit: 20, offset: 20, entity_sort: 'questions' }), expect.anything(), expect.anything())

  fireEvent.click(screen.getByRole('button', { name: '簇 21，查看问法' }))
  expect(screen.getByText('示例 21')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '簇 21，查看问答' }))
  expect(onDrill).toHaveBeenLastCalledWith({ cluster: 21 }, 'all')
  fireEvent.click(screen.getByRole('button', { name: '簇 21，未发现回复 2 条' }))
  expect(onDrill).toHaveBeenLastCalledWith({ cluster: 21 }, 'unanswered')

  fireEvent.change(screen.getByRole('searchbox', { name: '搜索规则问题簇' }), { target: { value: ' 未触发 ' } })
  fireEvent.click(screen.getByRole('button', { name: '搜索' }))
  await screen.findByText('第 1–1 条，共 1 条')
  expect(apiGet).toHaveBeenLastCalledWith('clusters', expect.objectContaining({ ...filters, limit: 20, offset: 0, entity_q: '未触发' }), expect.anything(), expect.anything())
  expect(screen.getByTitle('原场景回复未触发')).toBeTruthy()
  expect(screen.queryByText('示例 21')).toBeNull()
})

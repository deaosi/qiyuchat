import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { apiGet, type VocPlaybookResultsData } from '@/lib/api'
import { VocKnowledgeLibrary } from './voc-sop-workspace'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

const filters = { source: 'local', days: 7 } as const
const calls = (name: string) => vi.mocked(apiGet).mock.calls.filter(([route]) => route === name).map(([, params]) => params)
const source = { qa_id: 1, message_id: 'fixture-message', quote: '买家确认重新测试后已恢复。', ts: 1794441600000 }
const sops: VocPlaybookResultsData = {
  items: Array.from({ length: 10 }, (_, index) => ({
    id: `playbook-${index + 1}`, issue_id: `issue-${index + 1}`, title: `退款流程 ${index + 1}`,
    family: 'configuration', platform: '抖音', module: '退款', condition: '主账号',
    status: 'verified', checked_cases: 1, triage: [], missing: [], evidence: [],
    branches: [{ condition: '主账号', condition_evidence: [source], status: 'verified', case_count: 1,
      steps: [{ text: '调整退款触发条件。', evidence: [source] }], retest: '用相同退款场景复测。',
      retest_evidence: [source], result_evidence: [source], escalation: '' }],
  })),
  total: 10,
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('keeps the selected SOP page when the full scoped list arrives', async () => {
  let finish!: (data: VocPlaybookResultsData) => void
  const full = new Promise<VocPlaybookResultsData>(resolve => { finish = resolve })
  vi.mocked(apiGet).mockImplementation(async (_name, params) => params.first_page
    ? { ...sops, items: sops.items.slice(0, 8), first_page_total: 10 } : full)
  render(<VocKnowledgeLibrary filters={filters} onOpenPair={vi.fn()} />)

  expect(await screen.findByRole('button', { name: '查看 SOP · 退款流程 1' })).toBeTruthy()
  expect(calls('voc2/complete-sops')).toEqual([{ ...filters, first_page: 1 }])
  const pager = within(screen.getByRole('navigation', { name: 'SOP 分页' }))
  fireEvent.click(pager.getByRole('button', { name: '下一页' }))
  expect(pager.getByRole('button', { name: '第 2 页' }).getAttribute('aria-current')).toBe('page')
  expect(calls('voc2/complete-sops')).toEqual([{ ...filters, first_page: 1 }, filters])
  await act(async () => { finish(sops) })

  expect(screen.getByRole('button', { name: '查看 SOP · 退款流程 9' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: '查看 SOP · 退款流程 1' })).toBeNull()
  fireEvent.click(pager.getByRole('button', { name: '上一页' }))
  expect(screen.getByRole('button', { name: '查看 SOP · 退款流程 1' })).toBeTruthy()
  expect(calls('voc2/complete-sops')).toHaveLength(2)
})

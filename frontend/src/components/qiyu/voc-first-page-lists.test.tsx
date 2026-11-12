import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { apiGet, type VocIssuePilot, type VocPlaybookResultsData } from '@/lib/api'
import { VocFrequencyWorkspace } from './voc-next-workspace'
import { VocKnowledgeLibrary } from './voc-sop-workspace'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

const filters = { source: 'local', days: 7 } as const
const calls = (name: string) => vi.mocked(apiGet).mock.calls.filter(([route]) => route === name).map(([, params]) => params)
const source = { qa_id: 1, message_id: 'fixture-message', quote: '用户遇到的商品问题', ts: 1794441600000 }
const issues: VocIssuePilot = {
  items: Array.from({ length: 7 }, (_, index) => ({
    id: `issue-${index + 1}`, title: `产品异常 ${index + 1}`, family: 'anomaly', platform: '', module: '', condition: '',
    scope_status: 'scoped', evidence_status: 'reported', cases: [],
    impact: { consultations: 1, sessions: 1, merchants: 1, unidentified_sessions: 0 },
    next: { owner: '技术支持', action: '查明原因', missing: [] },
  })),
  total: 7, coverage: { source_events: 7, matched_consultations: 7, classified_consultations: 7, needs_context: 0 },
}
const linkedIssue = `issue_${'a'.repeat(24)}`
const playbooks: VocPlaybookResultsData = {
  items: Array.from({ length: 8 }, (_, index) => ({
    id: `playbook-${index + 1}`, issue_id: index === 7 ? linkedIssue : `issue-${index + 1}`,
    title: `问题汇总 ${index + 1}`, family: 'configuration', platform: '', module: '', condition: '',
    status: 'investigation', checked_cases: 0, branches: [], triage: [], missing: [], evidence: [],
  })),
  case_paths: [1, 2].map(id => ({
    id: `path-${id}`, qa_id: id, problem: { ...source, quote: `已有处理案例 ${id}` }, platform: '测试平台',
    outcome: 'not_recorded', steps: [{ actor: 'staff', state: 'reported', action: '核对设置', evidence: source }],
  })),
  total: 8,
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  history.replaceState(null, '', '/')
})

it('loads the full product list only on search and retains the entered search', async () => {
  let finish!: (data: VocIssuePilot) => void
  const full = new Promise<VocIssuePilot>(resolve => { finish = resolve })
  vi.mocked(apiGet).mockImplementation(async (name, params) => {
    if (name === 'voc2/issue-pilot') return params.first_page ? { ...issues, items: issues.items.slice(0, 6), first_page_total: 7 } : full
    if (name === 'voc2/topics') return { items: [], total: 0 }
    return { topic_count: 0, total_events: 0, source_pairs: 0, latest_job: null,
      semantics: { ready: true }, coverage: { processed_pairs: 0, pending_pairs: 0 } }
  })
  const handlers = { onOpenPair: vi.fn(), onOpenChat: vi.fn() }
  const workspace = render(<VocFrequencyWorkspace filters={filters} {...handlers} />)

  expect(await screen.findByRole('button', { name: /产品异常 1/ })).toBeTruthy()
  expect(calls('voc2/issue-pilot')).toEqual([{ ...filters, first_page: 1 }])
  const input = screen.getByRole<HTMLInputElement>('searchbox', { name: '搜索产品问题' })
  fireEvent.change(input, { target: { value: '产品异常 7' } })
  expect(calls('voc2/issue-pilot')).toEqual([{ ...filters, first_page: 1 }, filters])
  expect(screen.queryByText('未找到匹配的产品问题')).toBeNull()
  await act(async () => { finish(issues) })

  expect(screen.getByRole('searchbox', { name: '搜索产品问题' })).toBe(input)
  expect(input.value).toBe('产品异常 7')
  expect(screen.getByRole('button', { name: /产品异常 7/ })).toBeTruthy()

  workspace.rerender(<VocFrequencyWorkspace filters={{ ...filters, days: 30 }} {...handlers} />)
  expect(await screen.findByRole('button', { name: /产品异常 1/ })).toBeTruthy()
  workspace.rerender(<VocFrequencyWorkspace filters={filters} {...handlers} />)
  expect(await screen.findByRole('button', { name: /产品异常 1/ })).toBeTruthy()
  expect(screen.getByRole<HTMLInputElement>('searchbox', { name: '搜索产品问题' }).value).toBe('')
  expect(calls('voc2/issue-pilot').filter(params => !params.first_page)).toEqual([filters])
})

it('keeps the selected manual page when the full list arrives', async () => {
  let finish!: (data: VocPlaybookResultsData) => void
  const full = new Promise<VocPlaybookResultsData>(resolve => { finish = resolve })
  vi.mocked(apiGet).mockImplementation(async (_name, params) => params.first_page
    ? { ...playbooks, items: playbooks.items.slice(0, 6), first_page_total: 10 } : full)
  render(<VocKnowledgeLibrary filters={filters} onOpenPair={vi.fn()} />)

  expect(await screen.findByRole('button', { name: /已有处理案例 1/ })).toBeTruthy()
  expect(calls('voc2/playbooks')).toEqual([{ ...filters, first_page: 1 }])
  const pager = within(screen.getByRole('navigation', { name: '处理手册分页' }))
  fireEvent.click(pager.getByRole('button', { name: '下一页' }))
  expect(pager.getByText('第 2 / 2 页')).toBeTruthy()
  expect(calls('voc2/playbooks')).toEqual([{ ...filters, first_page: 1 }, filters])
  await act(async () => { finish(playbooks) })

  expect(pager.getByText('第 2 / 2 页')).toBeTruthy()
  expect(screen.getByRole('button', { name: /问题汇总 8/ })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /已有处理案例 1/ })).toBeNull()
  fireEvent.click(pager.getByRole('button', { name: '上一页' }))
  expect(screen.getByRole('button', { name: /已有处理案例 1/ })).toBeTruthy()
  expect(calls('voc2/playbooks')).toHaveLength(2)
})

it('loads the requested manual directly when linked from a product issue', async () => {
  history.replaceState(null, '', `/#voc-next?issue=${linkedIssue}`)
  vi.mocked(apiGet).mockResolvedValue(playbooks)
  render(<VocKnowledgeLibrary filters={filters} onOpenPair={vi.fn()} />)

  const dialog = within(await screen.findByRole('dialog', { name: '咨询详情' }))
  expect(dialog.getByText(/问题汇总 8/)).toBeTruthy()
  expect(calls('voc2/playbooks')).toEqual([filters])
})

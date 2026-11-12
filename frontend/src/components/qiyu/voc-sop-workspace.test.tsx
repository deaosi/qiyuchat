import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useApi } from '@/hooks/use-api'
import type { VocPilotEvidence, VocPlaybook, VocPlaybookBranch } from '@/lib/api'
import { VocKnowledgeLibrary } from './voc-sop-workspace'

vi.mock('@/hooks/use-api', () => ({ useApi: vi.fn() }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const ref = (message_id: string, quote: string, qa_id = 123): VocPilotEvidence => ({ message_id, quote, qa_id, ts: 1794441600000 })
const verified: VocPlaybookBranch = {
  id: 'branch-1', condition: '使用已授权的子账号。', status: 'verified',
  condition_evidence: [ref('m2', '当前店铺使用已授权的子账号。')],
  steps: [{ text: '开启指定场景并保存。', evidence: [ref('m5', '开启指定场景并保存。')] }],
  retest: '用同一句咨询重新测试回复。', retest_evidence: [ref('m6', '用同一句咨询重新测试回复。')],
  result_evidence: [ref('m7', '保存后重新测试，机器人回复正常。', 134)],
  escalation: '联系项目组另行排查。', escalation_evidence: [ref('m8', '联系项目组另行排查。')],
}

function playbook(id: string, title: string, branches: VocPlaybookBranch[]): VocPlaybook {
  return { id, issue_id: id, title, family: 'no_reply', platform: '拼多多', module: '场景回复', condition: '',
    status: 'verified', checked_cases: 1, triage: [], branches, missing: [], evidence: [] }
}

function mockList(items: VocPlaybook[]) {
  vi.mocked(useApi).mockImplementation((_name, filters, _key, enabled = true) => ({
    data: enabled ? filters.first_page ? { items: items.slice(0, 8), total: items.length, first_page_total: items.length }
      : { items, total: items.length } : null,
    loading: false, refreshing: false, error: null, retry: vi.fn(),
  }) as ReturnType<typeof useApi>)
}

it('only displays complete branches and opens and copies their exact procedure and result', async () => {
  const guidance = { ...verified, id: 'branch-2', status: 'guidance' as const, condition: '等待商家核对主账号。' }
  const falseVerified = { ...verified, id: 'branch-3', result_evidence: [], condition: '结果未确认。' }
  const complete = { ...playbook('procedure-1', '机器人场景未触发', [guidance, verified, falseVerified]), status: 'guidance' as const }
  const incomplete = playbook('procedure-2', '只有排查建议', [guidance, falseVerified])
  mockList([incomplete, complete])
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  const open = vi.fn()
  render(<VocKnowledgeLibrary filters={{ source: 'local', days: 7 }} onOpenPair={open} />)

  expect(screen.queryByText(incomplete.title)).toBeNull()
  expect(screen.queryByRole('tab')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: `查看 SOP · ${complete.title}` }))
  const drawer = within(screen.getByRole('dialog', { name: complete.title }))
  expect(drawer.getByText(verified.condition)).toBeTruthy()
  expect(drawer.getByText(verified.steps[0].text)).toBeTruthy()
  expect(drawer.getByText(verified.retest)).toBeTruthy()
  expect(drawer.getByText(verified.result_evidence![0].quote)).toBeTruthy()
  expect(drawer.queryByText(guidance.condition)).toBeNull()
  expect(drawer.getByText(verified.escalation)).toBeTruthy()

  const result = drawer.getByRole('heading', { name: '复测结果' }).closest('section')!
  const jump = within(result).getByRole('button', { name: '跳转到问答 134 的原话' })
  expect(jump.textContent?.trim()).toBe('跳转')
  fireEvent.click(jump)
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '134' }), 'm7')
  fireEvent.click(drawer.getByRole('button', { name: '复制 SOP' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining('复测结果 保存后重新测试，机器人回复正常。')))
  expect(writeText.mock.calls[0][0]).toContain(verified.escalation)
})

it('loads later SOP pages on demand and applies search across the complete list', () => {
  const items = Array.from({ length: 18 }, (_, index) => playbook(`sop-${index + 1}`, `场景案例 ${index + 1}`, [verified]))
  mockList(items)
  render(<VocKnowledgeLibrary filters={{ source: 'local', days: 7 }} onOpenPair={vi.fn()} />)

  const list = () => within(screen.getByRole('list', { name: 'SOP 案例' }))
  expect(list().getAllByRole('listitem')).toHaveLength(8)
  expect(vi.mocked(useApi).mock.calls.some(([, filters, , enabled]) => !filters.first_page && enabled)).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  expect(list().getAllByRole('listitem')).toHaveLength(8)
  expect(screen.getByRole('button', { name: '第 2 页' }).getAttribute('aria-current')).toBe('page')
  expect(screen.getByText('9–16 / 共 18 项')).toBeTruthy()
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索处理 SOP' }), { target: { value: '18' } })
  expect(list().getAllByRole('listitem')).toHaveLength(1)
  expect(list().getByRole('button', { name: '查看 SOP · 场景案例 18' })).toBeTruthy()
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索处理 SOP' }), { target: { value: '不存在的处理方法' } })
  expect(screen.getByText('未找到匹配的 SOP')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '清除搜索' }))
  expect(list().getAllByRole('listitem')).toHaveLength(8)
})

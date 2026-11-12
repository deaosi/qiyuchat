import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useApi } from '@/hooks/use-api'
import type { VocSop, VocSopEvidence } from '@/lib/api'
import { VocSopWorkspace } from './voc-sop-workspace'

vi.mock('@/hooks/use-api', () => ({ useApi: vi.fn() }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const ref = (message_id: string, quote: string): VocSopEvidence => ({ message_id, quote, ts: 1794441600000 })

it('opens exact conversation evidence and copies only reusable steps from a completed SOP', async () => {
  const complete: VocSop = {
    id: 'procedure-1', topic_id: 'topic-1', qa_id: 123, title: '机器人场景未触发', kind: 'anomaly', status: 'verified',
    problem: ref('m1', '已配置的场景没有回复。'), conditions: [ref('m2', '当前店铺使用子账号。')],
    steps: [
      { phase: 'action', evidence: ref('m3', '试过重新启动。'), outcome: 'failed', feedback: ref('m4', '仍然没有回复。') },
      { phase: 'action', evidence: ref('m5', '开启子账号权限后保存。'), outcome: 'verified' },
    ],
    result: ref('m6', '保存后重新测试，回复正常。'), missing: [],
  }
  const incomplete: VocSop = { ...complete, id: 'procedure-2', title: '待补复测的其他会话', status: 'incomplete', result: null, missing: ['result'] }
  vi.mocked(useApi).mockReturnValue({ data: { items: [incomplete, complete], total: 2 }, loading: false, refreshing: false, error: null, retry: vi.fn() })
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  const open = vi.fn()
  render(<VocSopWorkspace filters={{ source: 'local', days: 7 }} onOpenPair={open} />)

  fireEvent.click(screen.getByRole('button', { name: /机器人场景未触发/ }))
  const verified = within(screen.getByRole('dialog', { name: complete.title }))
  expect(verified.getByText('尝试未奏效')).toBeTruthy()
  const result = verified.getByRole('heading', { name: '复测结果' }).closest('section')!
  fireEvent.click(within(result).getByRole('button', { name: '定位原话' }))
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '123' }), 'm6')

  fireEvent.click(verified.getByRole('button', { name: '复制 SOP' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining('开启子账号权限后保存。')))
  expect(writeText).not.toHaveBeenCalledWith(expect.stringContaining('试过重新启动。'))
  fireEvent.click(verified.getByRole('button', { name: 'Close' }))
  fireEvent.click(screen.getByRole('button', { name: /待补复测的其他会话/ }))
  const pending = within(screen.getByRole('dialog', { name: incomplete.title }))
  expect(pending.queryByRole('button', { name: '复制 SOP' })).toBeNull()
  expect(pending.getByText('复测结果')).toBeTruthy()
  fireEvent.click(pending.getByRole('button', { name: 'Close' }))
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索处理 SOP' }), { target: { value: '回复正常' } })
  expect(within(screen.getByRole('list', { name: 'SOP 案例' })).getAllByRole('listitem')).toHaveLength(1)
})

it('opens the next page, resets to page one on search or filter, and supports empty statuses', () => {
  const items = Array.from({ length: 18 }, (_, index): VocSop => ({
    id: `sop-${index + 1}`, topic_id: '', qa_id: index + 1, title: `场景案例 ${index + 1}`,
    kind: 'configuration', status: 'incomplete', problem: ref(`m${index}`, '测试对话'),
    conditions: [], steps: [], result: null, missing: ['action'],
  }))
  vi.mocked(useApi).mockReturnValue({ data: { items, total: items.length }, loading: false, refreshing: false, error: null, retry: vi.fn() })
  render(<VocSopWorkspace filters={{ source: 'local', days: 7 }} onOpenPair={vi.fn()} />)

  const list = within(screen.getByRole('list', { name: 'SOP 案例' }))
  expect(list.getAllByRole('listitem')).toHaveLength(10)
  fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  expect(list.getAllByRole('listitem')).toHaveLength(8)
  expect(screen.getByRole('button', { name: '第 2 页' }).getAttribute('aria-current')).toBe('page')
  expect(screen.getByText('11–18 / 共 18 条')).toBeTruthy()
  expect(list.getByRole('button', { name: /场景案例 18/ })).toBeTruthy()
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索处理 SOP' }), { target: { value: '18' } })
  expect(list.getAllByRole('listitem')).toHaveLength(1)
  expect(screen.getByRole('button', { name: '第 1 页' }).getAttribute('aria-current')).toBe('page')
  fireEvent.click(screen.getByRole('button', { name: /效果已验证 0/ }))
  expect(screen.queryByRole('list', { name: 'SOP 案例' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '清除筛选' }))
  expect(within(screen.getByRole('list', { name: 'SOP 案例' })).getAllByRole('listitem')).toHaveLength(10)
})

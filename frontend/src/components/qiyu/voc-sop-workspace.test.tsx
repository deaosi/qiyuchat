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

  const [first, second] = screen.getAllByRole('article')
  const verifiedCard = within(first)
  expect(verifiedCard.getByRole('heading', { name: complete.title })).toBeTruthy()
  expect(verifiedCard.getByText('尝试未奏效')).toBeTruthy()
  const result = verifiedCard.getByRole('heading', { name: '复测结果' }).closest('section')!
  fireEvent.click(within(result).getByRole('button', { name: '定位原话' }))
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '123' }), 'm6')
  expect(within(second).queryByRole('button', { name: '复制 SOP' })).toBeNull()
  expect(within(second).getByText('待补充 复测结果')).toBeTruthy()

  fireEvent.click(verifiedCard.getByRole('button', { name: '复制 SOP' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining('开启子账号权限后保存。')))
  expect(writeText).not.toHaveBeenCalledWith(expect.stringContaining('试过重新启动。'))
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索处理 SOP' }), { target: { value: '回复正常' } })
  expect(screen.getAllByRole('article')).toHaveLength(1)
})

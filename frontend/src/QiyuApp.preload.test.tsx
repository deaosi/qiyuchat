import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { apiGet } from '@/lib/api'
import { requestActivity } from '@/lib/request-activity'
import QiyuApp from './QiyuApp'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

const responses: Record<string, unknown> = {
  meta: { total: 18, groups: [], staff: [] },
  summary: { qa_pair: 16, customer_count: 7, group_count: 3, unanswered: 2, recent: [] },
  trend: { days: 7, series: [], truncated: false, unknown_time: 0 },
  pairs: { items: [], total: 0 },
  'feishu-robot/access': { allowed: false },
  'voc2/topics': { items: [], total: 0, event_count: 0 },
  'voc2/complete-sops': { items: [], total: 0, first_page_total: 0 },
}

const queryCalls = (name: string) => vi.mocked(apiGet).mock.calls.filter(([endpoint]) => endpoint === name)
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('matchMedia', vi.fn(() => ({ addEventListener: vi.fn(), removeEventListener: vi.fn(), matches: false })))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(apiGet).mockImplementation(async name => responses[name] as never)
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  window.history.replaceState(null, '', '/')
})

it('starts the hovered VOC first page and joins it on click, then reuses overview data on return', async () => {
  let release!: (data: unknown) => void
  const topics = new Promise(resolve => { release = resolve })
  vi.mocked(apiGet).mockImplementation(async name => (name === 'voc2/topics' ? await topics : responses[name]) as never)
  render(<QiyuApp />)
  await advance(0)
  expect(screen.getByText('16')).toBeTruthy()
  expect(queryCalls('voc2/topics')).toHaveLength(0)

  const destination = screen.getByRole('button', { name: 'VOC高频问题' })
  fireEvent.mouseEnter(destination)
  await advance(200)
  expect(queryCalls('voc2/topics')).toHaveLength(1)
  expect(queryCalls('voc2/topics')[0][1]).toMatchObject({ days: 7, workbench: 1, offset: 0, limit: 20,
    trend: 'all', support_intent: 'all', diagnosis_status: 'all' })
  expect(queryCalls('voc2/topics')[0][3]).toEqual({ prefetch: true })

  fireEvent.click(destination)
  await act(async () => { release(responses['voc2/topics']) })
  expect(screen.getByText('当前范围暂无高频问题。')).toBeTruthy()
  expect(queryCalls('voc2/topics')).toHaveLength(1)

  fireEvent.click(screen.getByRole('button', { name: '会话概览' }))
  expect(screen.getByText('16')).toBeTruthy()
  await advance(0)
  expect(queryCalls('summary')).toHaveLength(1)
  expect(queryCalls('trend')).toHaveLength(1)
})

it('cancels a brief hover and warms the verified SOP first page when focused after foreground work', async () => {
  const finish = requestActivity.begin('当前页面正在读取')
  try {
    render(<QiyuApp />)
    const destination = screen.getByRole('button', { name: '沉淀知识库' })
    fireEvent.mouseEnter(destination)
    await advance(50)
    fireEvent.mouseLeave(destination)
    await advance(250)
    expect(queryCalls('voc2/complete-sops')).toHaveLength(0)

    fireEvent.focus(destination)
    await advance(200)
    expect(queryCalls('voc2/complete-sops')).toHaveLength(0)
    await act(async () => { finish() })
    expect(queryCalls('voc2/complete-sops')).toHaveLength(1)
    expect(queryCalls('voc2/complete-sops')[0][1]).toMatchObject({ days: 7, first_page: 1 })
    expect(queryCalls('voc2/complete-sops')[0][3]).toEqual({ prefetch: true })

    fireEvent.click(destination)
    expect(screen.getByText('当前范围暂无已验证的完整 SOP')).toBeTruthy()
    await advance(0)
    expect(queryCalls('voc2/complete-sops')).toHaveLength(1)
  } finally {
    finish()
  }
})

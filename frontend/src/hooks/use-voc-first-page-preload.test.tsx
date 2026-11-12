import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { apiGet } from '@/lib/api'
import { requestActivity } from '@/lib/request-activity'
import { useVocFirstPagePreload } from './use-voc-first-page-preload'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it('waits for the current view and only warms first-page requests for the other destination', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('requestIdleCallback', undefined)
  vi.mocked(apiGet).mockResolvedValue({ items: [] })
  const end = requestActivity.begin('正在读取当前页')
  try {
    const filters = { source: 'local', days: 7, group: 'idle-preload-test' } as const
    renderHook(() => useVocFirstPagePreload('voc-next', filters, 8))
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(apiGet).not.toHaveBeenCalled()
    end()
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(vi.mocked(apiGet).mock.calls.map(([endpoint, params]) => [endpoint, params.offset, params.first_page]))
      .toEqual([
        ['voc2/topics', 0, undefined],
        ['voc2/issue-pilot', undefined, 1],
        ['voc2/summary', undefined, undefined],
      ])
    expect(vi.mocked(apiGet).mock.calls.every(([, params, , activity]) =>
      params.group === filters.group && activity?.prefetch === true)).toBe(true)
  } finally {
    end()
  }
})

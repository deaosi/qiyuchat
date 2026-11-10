import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useBackgroundRefresh } from './use-background-refresh'

let visibility: 'visible' | 'hidden'
beforeEach(() => {
  vi.useFakeTimers()
  visibility = 'visible'
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

describe('VOC background refresh scheduling', () => {
  it('waits for slow requests to settle before scheduling the next update', () => {
    const refresh = vi.fn()
    const view = renderHook(({ fetching }) => useBackgroundRefresh(refresh, true, fetching), { initialProps: { fetching: true } })
    act(() => { vi.advanceTimersByTime(60000) })
    expect(refresh).not.toHaveBeenCalled()
    view.rerender({ fetching: false })
    act(() => { vi.advanceTimersByTime(5000) })
    expect(refresh).toHaveBeenCalledTimes(1)
    view.rerender({ fetching: true })
    act(() => { vi.advanceTimersByTime(60000) })
    expect(refresh).toHaveBeenCalledTimes(1)
    view.rerender({ fetching: false })
    act(() => { vi.advanceTimersByTime(5000) })
    expect(refresh).toHaveBeenCalledTimes(2)
  })

  it('checks for newly scheduled jobs at the slower idle interval', () => {
    const refresh = vi.fn()
    renderHook(() => useBackgroundRefresh(refresh, false, false))
    act(() => { vi.advanceTimersByTime(5000) })
    expect(refresh).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(25000) })
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('pauses hidden tabs and refreshes once on return', () => {
    const refresh = vi.fn()
    renderHook(() => useBackgroundRefresh(refresh, true, false))
    act(() => { visibility = 'hidden'; document.dispatchEvent(new Event('visibilitychange')) })
    act(() => { vi.advanceTimersByTime(60000) })
    expect(refresh).not.toHaveBeenCalled()
    act(() => { visibility = 'visible'; document.dispatchEvent(new Event('visibilitychange')) })
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('does not overlap an existing request when a tab becomes visible', () => {
    const refresh = vi.fn()
    renderHook(() => useBackgroundRefresh(refresh, true, true))
    act(() => { visibility = 'hidden'; document.dispatchEvent(new Event('visibilitychange')) })
    act(() => { visibility = 'visible'; document.dispatchEvent(new Event('visibilitychange')) })
    act(() => { vi.advanceTimersByTime(60000) })
    expect(refresh).not.toHaveBeenCalled()
  })

  it('removes pending timers and visibility listeners when leaving the workspace', () => {
    const refresh = vi.fn()
    const view = renderHook(() => useBackgroundRefresh(refresh, true, false))
    view.unmount()
    act(() => { vi.advanceTimersByTime(60000); document.dispatchEvent(new Event('visibilitychange')) })
    expect(refresh).not.toHaveBeenCalled()
  })
})

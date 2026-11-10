import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useDelayedBusy } from './use-delayed-busy'

beforeEach(() => vi.useFakeTimers())
afterEach(() => { cleanup(); vi.useRealTimers() })
const advance = (time: number) => act(() => { vi.advanceTimersByTime(time) })

describe('loading indicator timing', () => {
  it('does not flash for a fast response', () => {
    const view = renderHook(({ busy }) => useDelayedBusy(busy), { initialProps: { busy: true } })
    advance(80); view.rerender({ busy: false }); advance(500)
    expect(view.result.current).toBe(false)
  })

  it('keeps a slow request visible briefly without delaying its result', () => {
    const view = renderHook(({ busy }) => useDelayedBusy(busy), { initialProps: { busy: true } })
    advance(120); expect(view.result.current).toBe(true)
    view.rerender({ busy: false }); advance(120)
    expect(view.result.current).toBe(true)
    advance(120); expect(view.result.current).toBe(false)
  })

  it('does not blink between consecutive operations and cleans up on unmount', () => {
    const view = renderHook(({ busy }) => useDelayedBusy(busy), { initialProps: { busy: true } })
    advance(120); view.rerender({ busy: false }); advance(80)
    view.rerender({ busy: true }); advance(300)
    expect(view.result.current).toBe(true)
    view.rerender({ busy: false }); view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})

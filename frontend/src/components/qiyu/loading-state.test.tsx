import { Profiler } from 'react'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { requestActivity } from '@/lib/request-activity'
import { PageActivity, RefreshDataButton } from './loading-state'

beforeEach(() => vi.useFakeTimers())
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('loading indicator rendering', () => {
  it('does not commit the header progress indicator during background polling', () => {
    const commit = vi.fn()
    render(<Profiler id="header" onRender={commit}><PageActivity /></Profiler>)

    let finish!: () => void
    act(() => { finish = requestActivity.begin('后台轮询', true) })
    act(() => { vi.advanceTimersByTime(300) })
    act(() => { finish() })

    expect(commit).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status').getAttribute('data-visible')).toBe('false')
  })

  it('does not spin the refresh icon for a fast request', () => {
    render(<RefreshDataButton onRefresh={vi.fn()} />)
    const button = screen.getByRole('button', { name: '刷新数据' })
    const icon = button.querySelector('svg')!
    let finish!: () => void
    act(() => { finish = requestActivity.begin('正在读取数据…') })
    expect(button.getAttribute('aria-busy')).toBe('true')
    expect(icon.classList.contains('animate-spin')).toBe(false)
    act(() => { vi.advanceTimersByTime(80); finish() })
    act(() => { vi.advanceTimersByTime(300) })
    expect(button.getAttribute('aria-busy')).toBe('false')
    expect(icon.classList.contains('animate-spin')).toBe(false)
  })
})

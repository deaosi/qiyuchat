import { useCallback, useState } from 'react'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiGet } from '@/lib/api'
import { useApi } from './use-api'
import { useBackgroundRefresh } from './use-background-refresh'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers() })

describe('same-scope background updates', () => {
  it('continues polling when fast responses batch away intermediate loading states', async () => {
    vi.useFakeTimers()
    vi.mocked(apiGet).mockResolvedValue({ count: 12 })
    renderHook(() => {
      const [revision, setRevision] = useState(0)
      const query = useApi<{ count: number }>('voc2/summary', { source: 'local' }, revision)
      const refresh = useCallback(() => setRevision(value => value + 1), [])
      useBackgroundRefresh(refresh, true, query.loading || query.refreshing, revision)
      return query
    })
    await act(async () => { await Promise.resolve() })
    for (let cycle = 0; cycle < 3; cycle++) await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(apiGet).toHaveBeenCalledTimes(4)
  })

  it('keeps the mounted reading content and input while results are refreshed', async () => {
    const first = deferred<{ title: string }>()
    const second = deferred<{ title: string }>()
    vi.mocked(apiGet).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    function ReadingView({ revision }: { revision: number }) {
      const query = useApi<{ title: string }>('voc2/topics', { source: 'local' }, revision)
      const [draft, setDraft] = useState('')
      return <>{query.loading ? <p>正在读取</p> : query.data && <section><h2>{query.data.title}</h2><input aria-label="阅读备注" value={draft} onChange={event => setDraft(event.target.value)} /></section>}</>
    }
    const view = render(<ReadingView revision={0} />)
    await act(async () => { first.resolve({ title: '场景配置' }) })
    const input = screen.getByRole('textbox') as HTMLInputElement
    fireEvent.change(input, { target: { value: '正在阅读，保留这里' } })

    view.rerender(<ReadingView revision={1} />)
    expect(screen.queryByText('正在读取')).toBeNull()
    expect(screen.getByRole('heading').textContent).toBe('场景配置')
    expect(screen.getByRole('textbox')).toBe(input)
    expect(input.value).toBe('正在阅读，保留这里')

    await act(async () => { second.resolve({ title: '配置与排查' }) })
    expect(screen.getByRole('heading').textContent).toBe('配置与排查')
    expect(screen.getByRole('textbox')).toBe(input)
    expect(input.value).toBe('正在阅读，保留这里')
  })

  it('never displays another customer scope while its replacement is pending', async () => {
    const first = deferred<{ customer: string }>()
    const second = deferred<{ customer: string }>()
    vi.mocked(apiGet).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const view = renderHook(({ customer }) => useApi<{ customer: string }>('voc2/summary', { source: 'local', customer }), { initialProps: { customer: 'A' } })
    await act(async () => { first.resolve({ customer: 'A' }) })
    expect(view.result.current.data?.customer).toBe('A')
    view.rerender({ customer: 'B' })
    expect(view.result.current.data).toBeNull()
    expect(view.result.current.loading).toBe(true)
    await act(async () => { second.resolve({ customer: 'B' }) })
    expect(view.result.current.data?.customer).toBe('B')
  })

  it('ignores a late response from an obsolete filter scope', async () => {
    const first = deferred<{ customer: string }>()
    const second = deferred<{ customer: string }>()
    vi.mocked(apiGet).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const view = renderHook(({ customer }) => useApi<{ customer: string }>('voc2/summary', { source: 'local', customer }), { initialProps: { customer: 'A' } })
    view.rerender({ customer: 'B' })
    await act(async () => { second.resolve({ customer: 'B' }) })
    await act(async () => { first.resolve({ customer: 'A' }) })
    expect(view.result.current.data?.customer).toBe('B')
  })

  it('keeps the last same-scope result and reports a failed refresh', async () => {
    const first = deferred<{ count: number }>()
    const second = deferred<{ count: number }>()
    vi.mocked(apiGet).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const view = renderHook(({ revision }) => useApi<{ count: number }>('voc2/topics', { source: 'local' }, revision), { initialProps: { revision: 0 } })
    expect(vi.mocked(apiGet).mock.calls.at(-1)?.[3]).toEqual({ background: false })
    await act(async () => { first.resolve({ count: 12 }) })
    view.rerender({ revision: 1 })
    expect(vi.mocked(apiGet).mock.calls.at(-1)?.[3]).toEqual({ background: true })
    expect(view.result.current.refreshing).toBe(true)
    expect(view.result.current.loading).toBe(false)
    await act(async () => { second.reject(new Error('临时连接失败')) })
    expect(view.result.current.data?.count).toBe(12)
    expect(view.result.current.error).toBe('临时连接失败')
    expect(view.result.current.loading).toBe(false)
    expect(view.result.current.refreshing).toBe(false)
  })

  it.each([403, 404])('removes a result that is no longer accessible (%i)', async status => {
    const first = deferred<{ answer: string }>()
    const second = deferred<{ answer: string }>()
    vi.mocked(apiGet).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const view = renderHook(({ revision }) => useApi<{ answer: string }>('voc2/knowledge', { source: 'local', id: '1' }, revision), { initialProps: { revision: 0 } })
    await act(async () => { first.resolve({ answer: '旧知识' }) })
    view.rerender({ revision: 1 })
    await act(async () => { second.reject(new ApiError('当前知识已不可用', status)) })
    expect(view.result.current.data).toBeNull()
    expect(view.result.current.error).toBe('当前知识已不可用')
  })
})

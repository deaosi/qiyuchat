import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiGet, apiPost, downloadTemplate } from './api'
import { createRequestActivity, requestActivity } from './request-activity'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const config = { base_url: 'https://example.invalid/v1', model: 'test', configured: true, key_configured: true, timeout: 60, batch_size: 4 }
afterEach(() => { vi.unstubAllGlobals(); expect(requestActivity.getSnapshot().foreground).toBe(0); expect(requestActivity.getSnapshot().background).toBe(0) })

describe('shared loading feedback', () => {
  it('keeps the indicator while another request remains and releases it on failure', async () => {
    const first = deferred<Response>(), second = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise))
    const read = apiGet('gateway/config', { source: 'local' })
    const save = apiPost('gateway/config', { source: 'local' })
    expect(requestActivity.getSnapshot().foreground).toBe(2)
    first.resolve(Response.json(config)); await read
    expect(requestActivity.getSnapshot().foreground).toBe(1)
    second.reject(new Error('offline'))
    await expect(save).rejects.toThrow('offline')
  })

  it('finishes when switching pages aborts an outstanding request', async () => {
    vi.stubGlobal('fetch', vi.fn((_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')))
    })))
    const controller = new AbortController()
    const pending = apiGet('gateway/config', { source: 'local' }, controller.signal)
    expect(requestActivity.getSnapshot().foreground).toBe(1)
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('keeps background polling out of the foreground indicator', async () => {
    const result = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(result.promise))
    const pending = apiGet('gateway/config', { source: 'local' }, undefined, { background: true })
    expect(requestActivity.getSnapshot()).toEqual({ foreground: 0, background: 1, label: '' })
    result.resolve(Response.json(config)); await pending
  })

  it('keeps a download busy until the file body is read and reports errors', async () => {
    const body = deferred<Blob>()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: () => body.promise }))
    const pending = downloadTemplate()
    await Promise.resolve()
    expect(requestActivity.getSnapshot().foreground).toBe(1)
    body.reject(new Error('download interrupted'))
    await expect(pending).rejects.toThrow('download interrupted')
  })

  it.each([
    () => Response.json({ error: 'Unavailable' }, { status: 503 }),
    () => Response.json({ model: 'incomplete response' }),
    () => new Response('invalid json'),
  ])('finishes when HTTP, validation or response parsing fails', async response => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response()))
    await expect(apiGet('gateway/config', { source: 'local' })).rejects.toThrow()
  })

  it('does not count repeated cleanup twice or hide an older operation', () => {
    const store = createRequestActivity()
    const first = store.begin('导入'), second = store.begin('保存')
    second(); second()
    expect(store.getSnapshot()).toEqual({ foreground: 1, background: 0, label: '导入' })
    first()
    expect(store.getSnapshot().foreground).toBe(0)
  })
})

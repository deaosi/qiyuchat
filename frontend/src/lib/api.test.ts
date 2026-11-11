import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiGet, apiPost } from './api'
import { requestActivity } from './request-activity'

const filters = { source: 'local', id: '21' } as const
const staffMessage = {
  role: 'staff', content: '回复原文', ts: 1794358801000, time: '2026-11-11 09:00:01',
  source_message_id: 'official-staff-1', is_question: false, is_answer: false,
}

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => vi.unstubAllGlobals())

describe('VOC knowledge reuse response', () => {
  const item = {
    id: 'knowledge-1', topic_id: 'topic-1', question: '如何启用规则？', answer: '在后台启用规则。', scope: '',
    steps: [], reply_patterns: [], cautions: [], evidence: [], status: 'ready',
  }

  it.each([
    undefined, { level: 'guidance', reason_codes: ['source_confirmed'] },
    { level: 'verified', reason_codes: [], evidence: [{ quote: '打开后成功触发', role: 'merchant', message_id: 13, ts: 1794358801000 }] },
  ])('accepts optional reuse metadata %#', async knowledge_reuse => {
    const data = { items: [{ ...item, knowledge_reuse }], total: 1 }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(data)))
    await expect(apiGet('voc2/knowledge', filters)).resolves.toEqual(JSON.parse(JSON.stringify(data)))
  })

  it.each([
    null, { level: 'approved', reason_codes: [] }, { level: ['guidance'], reason_codes: [] },
    { level: 'reference', reason_codes: [1] }, { level: 'verified' },
    { level: 'verified', reason_codes: [], evidence: {} },
    { level: 'verified', reason_codes: [], evidence: [{ quote: true, role: 'merchant' }] },
    { level: 'verified', reason_codes: [], evidence: [{ quote: '成功', role: 'merchant', ts: false }] },
  ])('rejects malformed reuse metadata %#', async knowledge_reuse => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ item: { ...item, knowledge_reuse } })))
    await expect(apiGet('voc2/knowledge', filters)).rejects.toMatchObject({ status: 502 })
  })
})

describe('official reply lookup transport', () => {
  it('uses the existing POST authorization headers without registering foreground activity', async () => {
    let respond!: (response: Response) => void
    const fetch = vi.fn((_url: string, _request: RequestInit) => new Promise<Response>(resolve => { respond = resolve }))
    vi.stubGlobal('fetch', fetch)
    const signal = new AbortController().signal
    const result = apiPost('detail/replies', filters, undefined, undefined, signal)
    expect(requestActivity.getSnapshot()).toMatchObject({ foreground: 0, background: 1 })
    expect(fetch).toHaveBeenCalledWith('/api/detail/replies?source=local&id=21', expect.objectContaining({
      method: 'POST', credentials: 'same-origin', signal, body: new Uint8Array(),
    }))
    const request = fetch.mock.calls[0][1]
    expect(new Headers(request.headers).get('X-Local-Request')).toBe('1')
    const data = { status: 'found', messages: [staffMessage] }
    respond(json(data))
    await expect(result).resolves.toEqual(data)
    expect(requestActivity.getSnapshot()).toMatchObject({ foreground: 0, background: 0 })
  })

  it.each([
    { status: 'unknown', messages: [] },
    { status: 'found', messages: [] },
    { status: 'no_reply', messages: [staffMessage] },
    { status: 'found', messages: [{ ...staffMessage, role: 'user' }] },
    { status: 'found', messages: [{ ...staffMessage, ts: 0 }] },
    { status: 'found', messages: [{ ...staffMessage, is_answer: true }] },
    { status: 'found', messages: [staffMessage, staffMessage] },
  ])('rejects inconsistent lookup payload %#', async data => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(data)))
    await expect(apiPost('detail/replies', filters)).rejects.toMatchObject({ status: 502 })
  })
})

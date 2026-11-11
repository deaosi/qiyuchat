import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiGet, apiPost, type DetailRepliesResponse } from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'
import { QADetail } from './qa-detail'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn(), apiPost: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

function pair(id = '21'): QAPair {
  return { id, sessionId: `session-${id}`, clusterId: 1, question: `案例${id}原问题`, answer: '来源问答摘要',
    customerName: '合成客户', customerId: 'synthetic-customer', groupName: '合成群', staffName: '合成客服',
    askedAt: '2026-11-11T09:00:11+08:00', archived: false }
}

function message(index: number, content = `完整消息${index}`) {
  return { role: index % 2 ? 'staff' : 'user', content, source_message_id: `source-${index}`,
    ts: Date.parse('2026-11-11T09:00:00+08:00') + index * 1000,
    time: `2026-11-11 09:${String(Math.floor(index / 60)).padStart(2, '0')}:${String(index % 60).padStart(2, '0')}`,
    is_question: index === 0, is_answer: index === 1 }
}

function response(id = '21', messages = [message(0), message(1)], options: Record<string, unknown> = {}) {
  return { pair: { qa_id: Number(id), session_id: `session-${id}`, cluster_id: 1, question: `案例${id}完整客户原问题`,
    answer: '来源问答摘要', q_norm: '用于旧全局详情的归一化问法', asked_at: Date.parse('2026-11-11T09:00:11+08:00'),
    customer_name: '合成客户', customer_id: 'synthetic-customer', group_name: '合成群', staff_name: '合成客服' },
    messages, archived: false, message_count: messages.length, context_scope: 'session_customer', ...options }
}

function officialReply(index: number, content: string): DetailRepliesResponse['messages'][number] {
  return { ...message(index, content), role: 'staff', source_message_id: `official-${index}`, is_question: false, is_answer: false }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}

describe('embedded case conversation details', () => {
  const noStaffNotice = '这段已保存的会话记录中没有客服消息。'

  it('keeps the original question only in the timeline with all messages, roles and timestamps', async () => {
    const messages = Array.from({ length: 260 }, (_, index) => message(index))
    messages[0] = { ...messages[0], content: '案例21完整客户原问题' }
    messages[259] = { ...messages[259], source_message_id: '', is_question: true }
    vi.mocked(apiGet).mockResolvedValue(response('21', messages, { availability: 'available', can_lookup_replies: true }))
    render(<QADetail pair={pair()} embedded onClose={vi.fn()} />)

    await screen.findByText('完整消息259')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('heading', { name: '客户原问题' })).toBeNull()
    expect(screen.getByText('2026-11-11 09:00:11', { selector: 'dd' })).toBeTruthy()
    const transcript = screen.getByRole('list', { name: '会话消息' })
    expect(screen.getByText('案例21完整客户原问题')).toBe(within(transcript).getByText('案例21完整客户原问题'))
    expect(within(transcript).getAllByRole('listitem')).toHaveLength(260)
    expect(within(transcript).getAllByText('客户 · 合成客户')).toHaveLength(130)
    expect(within(transcript).getAllByText('客服 · 合成客服')).toHaveLength(130)
    expect(within(transcript).getByText('2026-11-11 09:00:00', { selector: 'time' })).toBeTruthy()
    expect(within(transcript).getAllByText('本次咨询')).toHaveLength(1)
    expect(screen.queryByText('归一化问题')).toBeNull()
    expect(screen.queryByText(noStaffNotice)).toBeNull()
    expect(screen.queryByRole('heading', { name: '客服回复' })).toBeNull()
    expect(vi.mocked(apiGet).mock.calls[0].slice(0, 2)).toEqual(['detail', { source: 'local', id: '21' }])
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('keeps the default global Sheet entry point', async () => {
    vi.mocked(apiGet).mockResolvedValue(response('21', [message(0)], { can_lookup_replies: true }))
    render(<QADetail pair={pair()} onClose={vi.fn()} />)
    expect(await screen.findByRole('dialog', { name: '问答与相关会话上下文' })).toBeTruthy()
    expect(screen.getByText('归一化问题')).toBeTruthy()
    expect(screen.getByRole('heading', { name: '客服回复' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: '客户原问题' })).toBeNull()
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('looks up eligible replies in the standard drawer only after the user requests them', async () => {
    const detail = response('21', [message(0, '客户原话')], { availability: 'available', can_lookup_replies: true })
    vi.mocked(apiGet).mockResolvedValueOnce({ ...detail, pair: { ...detail.pair, answer: '' } })
      .mockResolvedValueOnce(response('22', [message(0, '另一案例原话')], { availability: 'available', can_lookup_replies: true }))
    const reply = deferred<DetailRepliesResponse>()
    vi.mocked(apiPost).mockReturnValueOnce(reply.promise)
    const view = render(<QADetail pair={{ ...pair(), answer: '' }} onClose={vi.fn()} />)

    const lookup = await screen.findByRole('button', { name: '查询七鱼客服回复' })
    expect(apiPost).not.toHaveBeenCalled()
    fireEvent.click(lookup)
    expect(screen.getByText('客户原话')).toBeTruthy()
    expect(screen.getByText('正在查询七鱼客服回复…')).toBeTruthy()
    expect(apiPost).toHaveBeenCalledWith('detail/replies', { source: 'local', id: '21' }, undefined, undefined, expect.any(AbortSignal))
    await act(async () => { reply.resolve({ status: 'found', messages: [officialReply(1, '七鱼返回的人工原话')] }) })
    expect(screen.getByText('已查到人工客服回复')).toBeTruthy()
    expect(within(screen.getByRole('list', { name: '会话消息' })).getByText('七鱼返回的人工原话')).toBeTruthy()
    expect(screen.queryByText('未发现回复')).toBeNull()

    view.rerender(<QADetail pair={pair('22')} onClose={vi.fn()} />)
    expect(await screen.findByRole('button', { name: '查询七鱼客服回复' })).toBeTruthy()
    expect(screen.queryByText('七鱼返回的人工原话')).toBeNull()
    expect(apiPost).toHaveBeenCalledTimes(1)
  })

  it('clears old records immediately when changing cases and ignores a late response', async () => {
    const oldRequest = deferred<ReturnType<typeof response>>()
    const newRequest = deferred<ReturnType<typeof response>>()
    vi.mocked(apiGet).mockResolvedValueOnce(response('21', [message(0, '第一个案例记录')]))
      .mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(newRequest.promise)
    const view = render(<QADetail pair={pair('21')} embedded onClose={vi.fn()} />)
    await screen.findByText('第一个案例记录')
    view.rerender(<QADetail pair={pair('22')} embedded onClose={vi.fn()} />)
    expect(screen.queryByText('第一个案例记录')).toBeNull()
    expect(screen.getByText('正在读取相关会话上下文…')).toBeTruthy()
    view.rerender(<QADetail pair={pair('23')} embedded onClose={vi.fn()} />)
    await act(async () => { newRequest.resolve(response('23', [message(0, '当前案例记录')])) })
    await act(async () => { oldRequest.resolve(response('22', [message(0, '迟到的其他案例记录')])) })
    expect(screen.getByText('当前案例记录')).toBeTruthy()
    expect(screen.queryByText('迟到的其他案例记录')).toBeNull()
    expect(screen.getByText('session-23')).toBeTruthy()
    expect(vi.mocked(apiGet).mock.calls[1][2]?.aborted).toBe(true)
  })

  it.each(['available', 'partial'])('keeps the %s timeline visible and explains missing staff messages', async availability => {
    vi.mocked(apiGet).mockResolvedValue(response('21', [message(0), { ...message(1), role: 'bot', is_answer: false }], { availability }))
    render(<QADetail pair={pair()} embedded onClose={vi.fn()} />)
    expect(await screen.findByText(noStaffNotice)).toBeTruthy()
    const transcript = screen.getByRole('list', { name: '会话消息' })
    expect(within(transcript).getAllByRole('listitem')).toHaveLength(2)
    expect(within(transcript).getByText('机器人')).toBeTruthy()
    expect(within(transcript).getByText('2026-11-11 09:00:01')).toBeTruthy()
    expect(screen.queryByText('当前会话没有可展示的聊天记录。')).toBeNull()
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('shows the saved transcript during lookup and inserts official replies by timestamp without remounting it', async () => {
    const lookup = deferred<DetailRepliesResponse>()
    const first = message(0, '已保存的客户提问')
    const next = message(2, '已保存的后续问题')
    const sameTime = { ...next, source_message_id: 'same-time', content: '已保存的同一时刻补充' }
    vi.mocked(apiGet).mockResolvedValue(response('21', [first, next, sameTime], { availability: 'available', can_lookup_replies: true }))
    vi.mocked(apiPost).mockReturnValue(lookup.promise)
    render(<QADetail pair={pair()} embedded onClose={vi.fn()} />)

    await screen.findByText('正在查询七鱼客服回复…')
    const saved = screen.getByText(next.content)
    expect(screen.getByText(first.content)).toBeTruthy()
    expect(apiPost).toHaveBeenCalledWith('detail/replies', { source: 'local', id: '21' }, undefined, undefined, expect.any(AbortSignal))
    await act(async () => {
      lookup.resolve({ status: 'found', messages: [officialReply(2, '补取的同一时刻回复'), {
        ...officialReply(1, '补取的第一条回复'), images: [{ url: 'https://ysf.nosdn.127.net/official-reply.png' }],
      }] })
    })
    expect(screen.getByText('已补充七鱼返回的人工客服回复。')).toBeTruthy()
    expect(screen.getByText(next.content)).toBe(saved)
    const transcript = screen.getByRole('list', { name: '会话消息' })
    const bodies = within(transcript).getAllByRole('listitem').map(row => within(row).getByText(/^(已保存的|补取的)/).textContent)
    expect(bodies).toEqual([first.content, '补取的第一条回复', next.content, sameTime.content, '补取的同一时刻回复'])
    expect(within(transcript).getAllByText('人工客服')).toHaveLength(2)
    expect(within(transcript).getByText('2026-11-11 09:00:01')).toBeTruthy()
    expect(within(transcript).getByAltText('会话图片').getAttribute('src')).toBe('https://ysf.nosdn.127.net/official-reply.png')
    expect(screen.queryByText(noStaffNotice)).toBeNull()
  })

  it('renders inline attachments, supports enlargement and keeps readable text when an image fails', async () => {
    const url = 'https://ysf.nosdn.127.net/customer-photo.png'
    const messages = [
      { ...message(0, '[图片]'), images: [{ url }] },
      { ...message(1, '请看客服发来的截图。'), images: [{ url: 'https://ysf.nosdn.127.net/staff-photo.png' }] },
    ]
    vi.mocked(apiGet).mockResolvedValue(response('21', messages))
    render(<QADetail pair={pair()} embedded onClose={vi.fn()} />)

    const [thumbnail] = await screen.findAllByRole('button', { name: '放大会话图片' })
    const firstImage = within(thumbnail).getByAltText('会话图片')
    expect(firstImage.getAttribute('src')).toBe(url)
    expect(firstImage.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(screen.queryByText('[图片]')).toBeNull()
    fireEvent.click(thumbnail)
    expect(screen.getByRole('button', { name: '缩小会话图片' }).getAttribute('aria-expanded')).toBe('true')
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '缩小会话图片' }))
    const [, secondImage] = screen.getAllByAltText('会话图片')
    fireEvent.error(secondImage)
    expect(screen.getByText('图片暂不可查看')).toBeTruthy()
    expect(screen.getByText('请看客服发来的截图。')).toBeTruthy()
    expect(screen.getAllByAltText('会话图片')).toHaveLength(1)
  })

  it('aborts an old case lookup and ignores late replies after switching cases', async () => {
    const oldLookup = deferred<DetailRepliesResponse>()
    vi.mocked(apiGet).mockResolvedValueOnce(response('21', [message(0, '旧案例原文')], { can_lookup_replies: true }))
      .mockResolvedValueOnce(response('22', [message(0, '当前案例原文')], { can_lookup_replies: true }))
    vi.mocked(apiPost).mockReturnValueOnce(oldLookup.promise).mockResolvedValueOnce({ status: 'unmatched', messages: [] })
    const view = render(<QADetail pair={pair('21')} embedded onClose={vi.fn()} />)
    await screen.findByText('正在查询七鱼客服回复…')
    view.rerender(<QADetail pair={pair('22')} embedded onClose={vi.fn()} />)
    expect(await screen.findByText('未能在七鱼记录中匹配到这段会话。')).toBeTruthy()
    await act(async () => { oldLookup.resolve({ status: 'found', messages: [officialReply(1, '迟到的旧案例回复')] }) })
    expect(screen.getByText('当前案例原文')).toBeTruthy()
    expect(screen.queryByText('旧案例原文')).toBeNull()
    expect(screen.queryByText('迟到的旧案例回复')).toBeNull()
    expect(vi.mocked(apiPost).mock.calls[0][4]?.aborted).toBe(true)
    expect(vi.mocked(apiPost).mock.calls[1][1]).toEqual({ source: 'local', id: '22' })
    view.unmount()
    expect(vi.mocked(apiPost).mock.calls[1][4]?.aborted).toBe(true)
  })

  it('retries temporary lookup failures without rereading the saved conversation and distinguishes no reply', async () => {
    vi.mocked(apiGet).mockResolvedValue(response('21', [message(0)], { availability: 'available', can_lookup_replies: true }))
    vi.mocked(apiPost).mockResolvedValueOnce({ status: 'busy', messages: [] })
      .mockRejectedValueOnce(new Error('合成网络故障')).mockResolvedValueOnce({ status: 'no_reply', messages: [] })
    render(<QADetail pair={pair()} embedded onClose={vi.fn()} />)
    await screen.findByText('七鱼客服回复查询繁忙。')
    fireEvent.click(screen.getByRole('button', { name: '重试查询' }))
    await screen.findByText('暂时无法查询七鱼客服回复。')
    fireEvent.click(screen.getByRole('button', { name: '重试查询' }))
    expect(await screen.findByText('七鱼未返回该会话的人工客服回复。')).toBeTruthy()
    expect(screen.getByText('完整消息0')).toBeTruthy()
    expect(screen.queryByText(noStaffNotice)).toBeNull()
    expect(screen.queryByRole('button', { name: '重试查询' })).toBeNull()
    expect(apiGet).toHaveBeenCalledTimes(1)
  })

  it.each([
    { availability: 'partial', archived: false, messages: [message(0)], notice: '当前仅保留部分聊天记录。' },
    { availability: 'archived', archived: true, messages: [], notice: '原始聊天记录已归档。' },
    { availability: 'empty', archived: false, messages: [], notice: '当前会话没有可展示的聊天记录。' },
    { availability: undefined, archived: true, messages: [], notice: '原始聊天记录已归档。' },
  ])('shows source availability accurately for $availability / archived=$archived', async ({ availability, archived, messages, notice }) => {
    vi.mocked(apiGet).mockResolvedValue(response('21', messages, { availability, archived }))
    render(<QADetail pair={pair()} embedded onClose={vi.fn()} />)
    expect(await screen.findByText(notice)).toBeTruthy()
    if (messages.length) expect(screen.getByText('完整消息0')).toBeTruthy()
    else {
      expect(screen.queryByRole('list', { name: '会话消息' })).toBeNull()
      expect(screen.queryByText(noStaffNotice)).toBeNull()
    }
  })

  it('retries a failed read through the same QA detail endpoint', async () => {
    vi.mocked(apiGet).mockRejectedValueOnce(new Error('合成读取超时')).mockResolvedValueOnce(response())
    render(<QADetail pair={pair()} embedded onClose={vi.fn()} />)
    await screen.findByText('合成读取超时')
    fireEvent.click(screen.getByRole('button', { name: '重新加载' }))
    expect(await screen.findByText('完整消息1')).toBeTruthy()
    expect(screen.queryByText('合成读取超时')).toBeNull()
    expect(vi.mocked(apiGet).mock.calls).toHaveLength(2)
    expect(vi.mocked(apiGet).mock.calls[1].slice(0, 2)).toEqual(['detail', { source: 'local', id: '21' }])
  })
})

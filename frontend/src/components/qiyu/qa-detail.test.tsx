import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiGet } from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'
import { QADetail } from './qa-detail'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn() }))
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

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}

describe('embedded case conversation details', () => {
  it('renders all retained messages with roles, original time and question without a second dialog', async () => {
    const messages = Array.from({ length: 260 }, (_, index) => message(index))
    messages[259] = { ...messages[259], source_message_id: '', is_question: true }
    vi.mocked(apiGet).mockResolvedValue(response('21', messages))
    render(<QADetail pair={pair()} embedded onClose={vi.fn()} />)

    await screen.findByText('完整消息259')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByText('案例21完整客户原问题')).toBeTruthy()
    expect(screen.getByText('2026-11-11 09:00:11', { selector: 'dd' })).toBeTruthy()
    const transcript = screen.getByRole('list', { name: '会话消息' })
    expect(within(transcript).getAllByRole('listitem')).toHaveLength(260)
    expect(within(transcript).getAllByText('客户 · 合成客户')).toHaveLength(130)
    expect(within(transcript).getAllByText('客服 · 合成客服')).toHaveLength(130)
    expect(within(transcript).getByText('2026-11-11 09:00:00', { selector: 'time' })).toBeTruthy()
    expect(within(transcript).getAllByText('本次咨询')).toHaveLength(1)
    expect(screen.queryByText('归一化问题')).toBeNull()
    expect(screen.queryByRole('heading', { name: '客服回复' })).toBeNull()
    expect(vi.mocked(apiGet).mock.calls[0].slice(0, 2)).toEqual(['detail', { source: 'local', id: '21' }])
  })

  it('keeps the default global Sheet entry point', async () => {
    vi.mocked(apiGet).mockResolvedValue(response())
    render(<QADetail pair={pair()} onClose={vi.fn()} />)
    expect(await screen.findByRole('dialog', { name: '问答与相关会话上下文' })).toBeTruthy()
    expect(screen.getByText('归一化问题')).toBeTruthy()
    expect(screen.getByRole('heading', { name: '客服回复' })).toBeTruthy()
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
    expect(screen.getByText('案例23完整客户原问题')).toBeTruthy()
    expect(vi.mocked(apiGet).mock.calls[1][2]?.aborted).toBe(true)
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
    else expect(screen.queryByRole('list', { name: '会话消息' })).toBeNull()
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

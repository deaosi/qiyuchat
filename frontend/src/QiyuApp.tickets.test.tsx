import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import QiyuApp from './QiyuApp'

const id = '9007199254740993'
const ticket = { id, title: '订单复核', status: '处理中', priority: '高', owner: '测试客服',
  created_at: Date.UTC(2026, 0, 1, 1), updated_at: Date.UTC(2026, 0, 1, 3), linked_session_count: 1 }
const fetchApi = vi.fn(async (path: string) => {
  const url = new URL(path, location.origin)
  if (url.pathname === '/api/tickets') {
    const offset = Number(url.searchParams.get('offset'))
    return Response.json({ items: [ticket], total: 21, has_more: offset === 0, offset, limit: 20 })
  }
  if (url.pathname === '/api/tickets/detail') return Response.json({
    ticket: { ...ticket, description: '', fields: [
      { label: '平台&版本', value: '抖音 · Windows' },
      { label: '问题需求', value: '核对订单明细。' },
      { label: '附件/截图', value: '', images: [{ url: '/api/tickets/media?token=example', name: '订单截图' }] },
    ], attachments: [{ url: '/api/tickets/media?token=document', name: '订单明细.pdf' }] },
    history: [{ id: '783', action: '回复工单', operator: '测试客服', time: ticket.updated_at, details: [{ label: '回复', value: '处理已记录。' }] }],
    history_available: true,
    sessions: [{ session_id: 'linked-session', started_at: ticket.created_at, qa_id: 24 }],
  })
  if (url.pathname === '/api/detail') return Response.json({
    pair: { qa_id: 24, session_id: 'linked-session', question: '怎么核对？', answer: '请查看订单。' },
    messages: [{ role: 'staff', content: '请查看订单。', time: '2026-01-01 09:00:00' }],
    archived: false, context_scope: 'source_session', availability: 'available', can_lookup_replies: false,
  })
  return Response.json({ error: '未知接口' }, { status: 404 })
})

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ addEventListener: vi.fn(), removeEventListener: vi.fn(), matches: false })))
  vi.stubGlobal('fetch', fetchApi)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('七鱼工单', () => {
  it('opens the exact Feishu-linked ticket and opens a linked session without changing the route', async () => {
    history.replaceState(null, '', `#tickets?id=${id}`)
    render(<QiyuApp />)

    const popup = await screen.findByRole('dialog', { name: ticket.title })
    expect(popup.getAttribute('data-slot')).toBe('dialog-content')
    const detail = within(popup)
    expect(detail.getByText('核对订单明细。')).toBeTruthy()
    expect(detail.getByText('抖音 · Windows')).toBeTruthy()
    expect(detail.getByText('处理已记录。')).toBeTruthy()
    expect(detail.getByText(`七鱼工单 #${id} · 处理中`)).toBeTruthy()
    expect(fetchApi.mock.calls.some(([path]) => path.startsWith('/api/tickets/detail?') && new URL(path, location.origin).searchParams.get('id') === id)).toBe(true)

    fireEvent.click(detail.getByRole('button', { name: '查看会话' }))
    expect(await detail.findByText('请查看订单。')).toBeTruthy()
    expect(location.hash).toBe(`#tickets?id=${id}`)
    fireEvent.click(detail.getByRole('button', { name: 'Close' }))
    expect(location.hash).toBe('#tickets')

    const openDetails = await screen.findByRole('button', { name: `查看工单 ${id}` })
    expect(openDetails.textContent).toBe('查看详情')
    expect(openDetails.getAttribute('data-variant')).toBe('outline')
    expect(openDetails.querySelector('svg')).toBeNull()
    fireEvent.click(openDetails)
    expect(location.hash).toBe(`#tickets?id=${id}`)
    await screen.findByRole('dialog', { name: ticket.title })
    act(() => { history.replaceState(null, '', '#tickets'); window.dispatchEvent(new PopStateEvent('popstate')) })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('places uploaded files ahead of screenshots and opens the image preview directly', async () => {
    history.replaceState(null, '', `#tickets?id=${id}`)
    render(<QiyuApp />)
    const popup = await screen.findByRole('dialog', { name: ticket.title })
    const detail = within(popup)
    const screenshot = detail.getByRole('button', { name: '查看图片：订单截图' })
    const file = detail.getByRole('link', { name: '订单明细.pdf' })
    expect(file.hasAttribute('download')).toBe(true)
    expect(file.compareDocumentPosition(screenshot) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const image = within(screenshot).getByRole('img', { name: '订单截图' })
    expect(image.getAttribute('src')).toBe('/api/tickets/media?token=example')
    expect(detail.queryByText('查看原图')).toBeNull()
    expect(detail.queryByText('下载原图')).toBeNull()

    fireEvent.click(image)
    const preview = await screen.findByRole('dialog', { name: '工单图片预览' })
    expect(within(preview).getByRole('slider', { name: '缩放比例' })).toBeTruthy()
    fireEvent.keyDown(preview, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: '工单图片预览' })).toBeNull()
    expect(screen.getByRole('dialog', { name: ticket.title })).toBe(popup)
    expect(location.hash).toBe(`#tickets?id=${id}`)
  })

  it('sends only work order filters and keeps them when turning the page', async () => {
    history.replaceState(null, '', '#tickets')
    render(<QiyuApp />)
    expect(await screen.findByRole('heading', { name: '七鱼工单' })).toBeTruthy()
    const form = within(screen.getByRole('search', { name: '筛选工单' }))
    fireEvent.change(form.getByLabelText('工单搜索'), { target: { value: '订单' } })
    fireEvent.change(form.getByLabelText('创建日期从'), { target: { value: '2026-01-01' } })
    fireEvent.change(form.getByLabelText('创建日期至'), { target: { value: '2026-01-03' } })
    fireEvent.click(form.getByRole('button', { name: '查询' }))
    fireEvent.click(await screen.findByRole('button', { name: '下一页' }))
    expect(await screen.findByText('第 21–21 条，共 21 条')).toBeTruthy()
    const params = new URL(fetchApi.mock.calls.at(-1)![0], location.origin).searchParams
    expect(Object.fromEntries(params)).toEqual({ source: 'local', q: '订单', start: '2026-01-01', end: '2026-01-03', offset: '20', limit: '20' })
  })
})

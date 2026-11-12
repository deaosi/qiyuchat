import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { QATable } from './qa-table'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('sends only the settled search on page one when typing on a later page', async () => {
  const rows = Array.from({ length: 18 }, (_, index) => ({ qa_id: index + 1, question: `客户问题 ${index + 1}`, answer: '' }))
  const fetch = vi.fn(async (url: string) => {
    const params = new URL(url, location.origin).searchParams
    const offset = Number(params.get('offset'))
    const matches = rows.filter(row => row.question.includes(params.get('q') || ''))
    return Response.json({ items: matches.slice(offset, offset + 8), total: matches.length, limit: 8, offset })
  })
  vi.stubGlobal('fetch', fetch)
  const open = vi.fn()
  render(<QATable data={[]} remote={{ filters: { source: 'local' } }} onSelect={open} />)
  await screen.findByText('第 1 / 3 页')
  fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  await screen.findByText('第 2 / 3 页')
  fireEvent.change(screen.getByRole('textbox', { name: '搜索问答' }), { target: { value: '客户问题 18' } })
  expect(fetch).toHaveBeenCalledTimes(2)

  await screen.findByText('第 1 / 1 页')
  expect(screen.getByRole('button', { name: '客户问题 18' })).toBeTruthy()
  expect(fetch).toHaveBeenCalledTimes(3)
  const search = new URL(fetch.mock.calls[2][0], location.origin).searchParams
  expect([search.get('q'), search.get('offset')]).toEqual(['客户问题 18', '0'])
  const jump = screen.getByRole('button', { name: '跳转到问答 18 的会话详情' })
  expect(jump.textContent).toBe('跳转')
  fireEvent.click(jump)
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '18' }))
})

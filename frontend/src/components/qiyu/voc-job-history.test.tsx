import { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { VocJobHistory } from './voc-job-history'

function HistoryTrigger() {
  const [open, setOpen] = useState(false)
  return <VocJobHistory open={open} onOpenChange={setOpen} trigger={<button type="button">查看整理记录</button>} />
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('reads records only after opening and distinguishes partial, failed and active jobs', async () => {
  const base = { total: 10, processed: 7, failed: 2, skipped: 1, usage_tokens: 0, error: '', created_at: '2026-11-11 15:00:00', updated_at: '2026-11-11 15:01:00' }
  const fetch = vi.fn((_url: string, _options: RequestInit) => Promise.resolve(Response.json({ items: [
    { ...base, id: 'active', status: 'running' },
    { ...base, id: 'partial', status: 'partial', error: '部分问答整理失败。' },
    { ...base, id: 'failed', status: 'failed', processed: 0, failed: 10 },
  ] })))
  vi.stubGlobal('fetch', fetch)
  render(<HistoryTrigger />)
  expect(fetch).not.toHaveBeenCalled()
  const trigger = screen.getByRole('button', { name: '查看整理记录' })
  fireEvent.click(trigger)

  const [active, partial, failed] = within(await screen.findByRole('list', { name: '模型整理任务' })).getAllByRole('listitem')
  expect(within(active).getByText(/最近更新/)).toBeTruthy()
  expect(within(partial).getByText('部分完成')).toBeTruthy()
  expect(within(partial).getByText('7')).toBeTruthy()
  expect(within(partial).getByText('部分问答整理失败。')).toBeTruthy()
  expect(within(failed).getByText('失败', { selector: 'span' })).toBeTruthy()
  expect(within(failed).getByText(/结束/)).toBeTruthy()
  expect(fetch.mock.calls.map(([url, options]) => [url, options.method ?? 'GET'])).toEqual([['/api/voc2/jobs?source=local', 'GET']])

  fireEvent.keyDown(screen.getByRole('button', { name: '关闭' }), { key: 'Escape', code: 'Escape' })
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('retries a failed read through the same read-only endpoint', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json({ error: '模型整理服务暂不可用。' }, { status: 503 }))
    .mockResolvedValueOnce(Response.json({ items: [] }))
  vi.stubGlobal('fetch', fetch)
  render(<VocJobHistory open onOpenChange={vi.fn()} />)

  expect((await screen.findByRole('alert')).textContent).toContain('模型整理服务暂不可用。')
  fireEvent.click(screen.getByRole('button', { name: '重试读取' }))
  expect(await screen.findByText('暂无模型整理记录')).toBeTruthy()
  expect(fetch.mock.calls.map(([url, options]) => [url, options.method ?? 'GET'])).toEqual([
    ['/api/voc2/jobs?source=local', 'GET'],
    ['/api/voc2/jobs?source=local', 'GET'],
  ])
})

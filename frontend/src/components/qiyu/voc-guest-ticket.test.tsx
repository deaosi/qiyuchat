import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { VocGuestTicket } from './voc-guest-ticket'

it('renders the shared ticket with only images scoped to the open guest link', () => {
  const previousPath = window.location.pathname
  const path = `/guest/ticket/${'a'.repeat(43)}`
  const imageUrl = `${path}/images/${'1'.repeat(64)}`
  window.history.replaceState(null, '', path)
  try {
    render(<VocGuestTicket kind="ticket" ticket={{
      title: '商品答复异常', status: '处理中', priority: '一般', description: '',
      created_at: 1794615600000, updated_at: null,
      fields: [
        { label: '问题需求', value: '机器人未按设置回复。' },
        { label: '附件/截图', value: '', images: [
          { name: '工单截图', url: imageUrl },
          { name: '私有接口', url: '/api/tickets/media?token=private' },
          { name: '其他工单', url: `/guest/ticket/${'b'.repeat(43)}/images/${'1'.repeat(64)}` },
        ] },
      ],
      images: [{ name: '重复工单截图', url: imageUrl }],
    }} />)

    expect(screen.getByRole('heading', { name: '商品答复异常' })).toBeTruthy()
    expect(screen.getByText('机器人未按设置回复。')).toBeTruthy()
    expect(screen.getAllByRole('img')).toHaveLength(1)
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByRole('img', { name: '工单截图' }).getAttribute('src')).toBe(imageUrl)

    fireEvent.click(screen.getByRole('button', { name: '查看图片：工单截图' }))
    const preview = within(screen.getByRole('dialog', { name: '工单图片预览' }))
    expect(preview.getByRole('img', { name: '工单截图' }).getAttribute('src')).toBe(imageUrl)
    expect(preview.getByRole('slider', { name: '缩放比例' })).toBeTruthy()
  } finally {
    cleanup()
    window.history.replaceState(null, '', previousPath)
  }
})

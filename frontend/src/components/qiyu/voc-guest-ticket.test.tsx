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

it('shows the ticket-scoped download, technical result, and conversation without administrative navigation', () => {
  const previousPath = window.location.pathname
  const path = `/guest/ticket/${'a'.repeat(43)}`
  const fileUrl = `${path}/attachments/${'0'.repeat(64)}`
  window.history.replaceState(null, '', path)
  try {
    render(<VocGuestTicket kind="ticket" ticket={{
      id: '58', title: '打印故障', status: '处理中', priority: '一般', description: '',
      created_at: null, updated_at: null, attachments: [{ name: '诊断日志.zip', url: fileUrl }],
    }} history_available history={[]}
    technical_feedback={{
      status: '已修复', reply: '重新安装后恢复', resolved_version: '2.0',
      received_at: null, writeback_status: 'succeeded', written_at: null,
    }} sessions={[{
      started_at: null, customer_name: '店铺小许', messages: [
        { text: '打印为空白', time: '10:00', role: 'user', sender_name: '店铺小许' },
        { text: '请确认驱动版本', time: '10:01', role: 'staff', sender_name: '在线客服' },
      ],
    }]} />)
    expect(screen.getByText('重新安装后恢复')).toBeTruthy()
    expect(screen.getByText('打印为空白').closest('li')?.dataset.side).toBe('left')
    expect(screen.getByText('请确认驱动版本').closest('li')?.dataset.side).toBe('right')
    expect(screen.getAllByRole('link').map(link => link.getAttribute('href'))).toEqual([fileUrl])
    expect(screen.queryByRole('button', { name: '编辑工单' })).toBeNull()
  } finally {
    cleanup()
    window.history.replaceState(null, '', previousPath)
  }
})

it('focuses the follow-up attachment heading and displays only attachments for the current link', () => {
  const previousUrl = window.location.pathname + window.location.hash
  const token = 'a'.repeat(43)
  const path = `/guest/ticket/${token}`
  const fileUrl = `${path}/attachments/${'0'.repeat(64)}`
  window.history.replaceState(null, '', `${path}#ticket-followup-attachments`)
  try {
    render(<VocGuestTicket kind="ticket" ticket={{
      title: '打印故障', status: '处理中', priority: '一般', description: '',
      created_at: null, updated_at: null,
    }} followup_attachments={[{
      time: 1794537200000, images: [], attachments: [
        { name: '跟进附件.pdf', url: fileUrl },
        { name: '其他工单.pdf', url: fileUrl.replace(token, 'b'.repeat(43)) },
      ],
    }]} />)
    const heading = screen.getByRole('heading', { name: '跟进附件' })
    expect(document.activeElement).toBe(heading)
    const section = screen.getByRole('region', { name: '跟进附件' })
    expect(within(section).getByRole('link', { name: '跟进附件.pdf' }).getAttribute('href')).toBe(fileUrl)
    expect(within(section).queryByRole('link', { name: '其他工单.pdf' })).toBeNull()
  } finally {
    cleanup()
    window.history.replaceState(null, '', previousUrl)
  }
})

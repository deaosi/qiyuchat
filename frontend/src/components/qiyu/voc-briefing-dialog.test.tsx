import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiGet, apiPost, type ApiFilters, type FeishuPublication } from '@/lib/api'
import { VocBriefingDialog } from './voc-briefing-dialog'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiGet: vi.fn(), apiPost: vi.fn() }))

const start = '2026-11-05'
const end = '2026-11-11'
const newId = `t_${'a'.repeat(24)}`
const risingId = `t_${'b'.repeat(24)}`
const scope: ApiFilters = { source: 'local', days: 7, group: '内部客户群', staff: '内部客服' }
const comparison = {
  current: { start, end, source_pairs: 12, processed_pairs: 10 },
  previous: { start: '2026-10-29', end: '2026-11-04', source_pairs: 10, processed_pairs: 9 },
  comparable: true, note: '', last_synced_at: '',
}
const topics = {
  new: [{ id: newId, theme: '新主题 [测试]', consultation_count: 1, trend: { status: 'new', delta: 1 }, samples: ['原始会话不要出现'] }],
  rising: [{ id: risingId, theme: '升温主题', consultation_count: 7, trend: { status: 'rising', delta: 5 }, samples: ['原始会话不要出现'] }],
}
const knowledge = (id: string, title: string, level: string, time: string) => ({
  id, topic_id: newId, question: title, status: 'ready', knowledge_reuse: { level },
  updated_at: time, answer: '客服原话不要出现', evidence: [{ question: '聊天内容不要出现' }],
})
const writeText = vi.fn(async (_text: string) => undefined)
const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

beforeEach(() => {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  vi.mocked(apiGet).mockImplementation(async (name, filters) => {
    if (name === 'voc2/topics') {
      const items = filters.trend === 'rising' ? topics.rising : topics.new
      return { items, total: items.length, comparison }
    }
    if (name === 'voc2/knowledge') return { items: [
      knowledge('new-guidance', '已核验的新指引 https://example.invalid/private', 'guidance', '2026-11-04T16:00:00Z'),
      knowledge('new-verified', '已验证的处理办法', 'verified', '2026-11-11 23:59:59'),
      knowledge('too-early', '上期知识', 'verified', '2026-11-04T15:59:59Z'),
      knowledge('too-late', '下期知识', 'verified', '2026-11-11T16:00:00Z'),
      knowledge('reference', '待核验记录', 'reference', '2026-11-07 08:00:00'),
      knowledge('unknown', '未知日期记录', 'verified', '2026-11-31 08:00:00'),
    ] }
    throw new Error(`Unexpected query ${name}`)
  })
})

afterEach(() => {
  cleanup()
  if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
  else Reflect.deleteProperty(navigator, 'clipboard')
  vi.clearAllMocks()
})

describe('VOC Feishu briefing preview', () => {
  it('uses the observed period for all rows and only copies qualified summary data', async () => {
    render(<VocBriefingDialog open onOpenChange={vi.fn()} filters={scope} />)

    await screen.findByText('已验证的处理办法')
    expect(screen.getByText('新主题 [测试]')).toBeTruthy()
    expect(screen.getByText('升温主题')).toBeTruthy()
    expect(screen.getByText('已核验的新指引 [链接]')).toBeTruthy()
    expect(screen.queryByText('上期知识')).toBeNull()
    expect(screen.queryByText('下期知识')).toBeNull()
    expect(screen.queryByText('待核验记录')).toBeNull()
    expect(screen.queryByText('未知日期记录')).toBeNull()
    expect(vi.mocked(apiGet).mock.calls.map(([name, filters]) => [name, filters])).toEqual([
      ['voc2/topics', { ...scope, workbench: 1, trend: 'new', limit: 20, offset: 0 }],
      ['voc2/topics', { ...scope, days: 0, start, end, workbench: 1, trend: 'rising', limit: 20, offset: 0 }],
      ['voc2/knowledge', { ...scope, days: 0, start, end }],
    ])

    fireEvent.click(screen.getByRole('button', { name: '复制 Markdown 摘要' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    const copied = writeText.mock.calls[0][0]
    expect(copied).toContain(`${start} 至 ${end}`)
    expect(copied).toContain('新主题 \\[测试\\]')
    expect(copied).toContain('较上期增加 5 次')
    expect(copied).toContain(`[查看问题](${window.location.origin}/#questions?topic=${newId}&start=${start}&end=${end})`)
    expect(copied).not.toMatch(/内部客户群|内部客服|不要出现|待核验记录|下期知识|上期知识|example\.invalid/)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('explains insufficient trend coverage and still shows qualified knowledge updates', async () => {
    const normalQuery = vi.mocked(apiGet).getMockImplementation()!
    vi.mocked(apiGet).mockImplementation((name, filters, ...rest) => name === 'voc2/topics'
      ? Promise.resolve({ items: [], total: 0, comparison: { ...comparison, comparable: false, note: '已整理样本不足。' } })
      : normalQuery(name, filters, ...rest))
    render(<VocBriefingDialog open onOpenChange={vi.fn()} filters={scope} />)
    await screen.findByText('已验证的处理办法')
    expect(screen.getByText('趋势暂不可判断。')).toBeTruthy()
    expect(screen.getByText('已整理样本不足。')).toBeTruthy()
    expect(screen.queryByText('明显上升的问题')).toBeNull()
    expect(vi.mocked(apiGet).mock.calls.map(([name]) => name)).toEqual(['voc2/topics', 'voc2/knowledge'])
  })

  it('does no background read while closed and skips queries for an unbounded range', async () => {
    const view = render(<VocBriefingDialog open={false} onOpenChange={vi.fn()} filters={scope} />)
    expect(apiGet).not.toHaveBeenCalled()
    view.rerender(<VocBriefingDialog open onOpenChange={vi.fn()} filters={{ ...scope, days: 0 }} />)
    expect(await screen.findByText('选择最近一段时间或填写完整起止日期后，即可预览本期成果。')).toBeTruthy()
    expect(apiGet).not.toHaveBeenCalled()
    view.rerender(<VocBriefingDialog open onOpenChange={vi.fn()} filters={{ ...scope, start }} />)
    expect(apiGet).not.toHaveBeenCalled()
    expect((screen.getByRole('button', { name: '复制 Markdown 摘要' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.mouseDown(screen.getByRole('tab', { name: '发布到飞书' }), { button: 0 })
    expect(screen.getByText(/发布需要明确的统计区间/)).toBeTruthy()
    expect(apiGet).not.toHaveBeenCalled()
    expect((screen.getByRole('button', { name: '发布到飞书' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('disables copying when a read fails and retries the failed source', async () => {
    vi.mocked(apiGet).mockRejectedValueOnce(new Error('network failure'))
    render(<VocBriefingDialog open onOpenChange={vi.fn()} filters={scope} />)
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect((screen.getByRole('button', { name: '复制 Markdown 摘要' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('升温主题')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('previews only on request, defaults to SOP, and keeps the receipt tied to its published scope', async () => {
    const destination = 'https://example.feishu.cn/base/reports?table=cases'
    const regularRead = vi.mocked(apiGet).getMockImplementation()!
    vi.mocked(apiGet).mockImplementation((name, filters, ...rest) => name === 'voc2/feishu/preview' ? Promise.resolve({
      title: filters.kind === 'sops' ? '本期操作知识' : '本期高频问题',
      count: 1, total: 2, items: [{ title: filters.kind === 'sops' ? '触发排查步骤' : '为什么无法触发', summary: '脱敏摘要' }],
      start, end, fingerprint: `approved-${filters.kind}`, publish_configured: true, destination,
    }) : regularRead(name, filters, ...rest))
    let complete!: (receipt: FeishuPublication) => void
    vi.mocked(apiPost).mockReturnValueOnce(new Promise<FeishuPublication>(resolve => { complete = resolve }))
    render(<VocBriefingDialog open onOpenChange={vi.fn()} filters={scope} defaultKind="sops" />)

    await screen.findByText('已验证的处理办法')
    expect(vi.mocked(apiGet).mock.calls.some(([name]) => name === 'voc2/feishu/preview')).toBe(false)
    fireEvent.mouseDown(screen.getByRole('tab', { name: '发布到飞书' }), { button: 0 })
    await screen.findByText('触发排查步骤')
    expect(screen.getByRole('button', { name: '知识 SOP' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('link', { name: destination }).getAttribute('href')).toBe(destination)
    expect(within(screen.getByRole('tabpanel', { name: '发布到飞书' })).getByText(`${start} 至 ${end}`)).toBeTruthy()
    const publish = screen.getByRole('button', { name: '发布到飞书' })
    fireEvent.click(publish)
    fireEvent.click(publish)
    expect(apiPost).toHaveBeenCalledExactlyOnceWith('voc2/feishu/publish', { ...scope, kind: 'sops', start: undefined, end: undefined },
      JSON.stringify({ fingerprint: 'approved-sops' }), { 'Content-Type': 'application/json' })

    fireEvent.click(screen.getByRole('button', { name: '高频问题报告' }))
    await screen.findByText('为什么无法触发')
    await act(async () => complete({ url: destination, title: '本期操作知识', reused: false, count: 1 }))
    expect(screen.queryByRole('link', { name: '在飞书查看成果' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '知识 SOP' }))
    expect((await screen.findByRole('link', { name: '在飞书查看成果' })).getAttribute('href')).toBe(destination)
    expect((screen.getByRole('button', { name: '发布到飞书' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('keeps publishing unavailable when the environment is unconfigured or the preview is empty', async () => {
    const regularRead = vi.mocked(apiGet).getMockImplementation()!
    vi.mocked(apiGet).mockImplementation((name, filters, ...rest) => name === 'voc2/feishu/preview' ? Promise.resolve({
      title: '飞书发布预览', start, end, fingerprint: `approved-${filters.kind}`,
      ...(filters.kind === 'topics'
        ? { count: 1, total: 1, items: [{ title: '待发布问题', summary: '摘要' }], publish_configured: false, content: '<root/>' }
        : { count: 0, total: 0, items: [], publish_configured: true }),
    }) : regularRead(name, filters, ...rest))
    render(<VocBriefingDialog open onOpenChange={vi.fn()} filters={scope} />)
    fireEvent.mouseDown(screen.getByRole('tab', { name: '发布到飞书' }), { button: 0 })
    await screen.findByText('当前环境未配置飞书发布。')
    expect((screen.getByRole('button', { name: '发布到飞书' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByRole('button', { name: '下载 XML' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '知识 SOP' }))
    await screen.findByText('当前范围没有可发布的知识 SOP。')
    expect((screen.getByRole('button', { name: '发布到飞书' }) as HTMLButtonElement).disabled).toBe(true)
    expect(apiPost).not.toHaveBeenCalled()
  })
})

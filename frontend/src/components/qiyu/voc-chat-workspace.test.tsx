import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ApiFilters } from '@/lib/api'
import { createChatStore, type ChatAnswer } from '@/lib/voc-chat'
import { VocChatWorkspace } from './voc-chat-workspace'

vi.mock('@/hooks/use-api', () => ({
  useApi: () => ({ data: null, loading: false, error: null }),
}))

afterEach(cleanup)

function setup(filters: ApiFilters, overrides: Partial<ChatAnswer> = {}) {
  const answer: ChatAnswer = { answer: '示例答复', citations: [], scope: { label: '近 7 天' }, model: 'test', tools: [], ...overrides }
  const transport = vi.fn().mockResolvedValue(answer)
  const store = createChatStore({ transport })
  render(<VocChatWorkspace filters={filters} onScopeChange={vi.fn()} store={store} />)
  return { transport, input: screen.getByRole<HTMLTextAreaElement>('textbox', { name: '向语海Voc提问' }) }
}

it('rotates the empty placeholder and fills the question currently shown', () => {
  vi.useFakeTimers()
  try {
    const { transport, input } = setup({ source: 'local', days: 7 })
    input.focus()
    expect(input.placeholder).toBe('当前范围的高频咨询有哪些？')

    act(() => vi.advanceTimersByTime(5000))
    const shown = '客服通常怎么回复场景未触发的问题？'
    expect(input.placeholder).toBe(shown)
    expect(input.value).toBe('')

    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input.value).toBe(shown)
    expect(transport).not.toHaveBeenCalled()

    act(() => vi.advanceTimersByTime(10000))
    expect(input.value).toBe(shown)
    expect(input.placeholder).toBe(shown)
    fireEvent.change(input, { target: { value: '' } })
    act(() => vi.advanceTimersByTime(5000))
    expect(input.placeholder).toBe('哪些答案已通过会话验证，可以直接复用？')
  } finally {
    vi.useRealTimers()
  }
})

it.each([
  [undefined, '当前范围的高频咨询有哪些？'],
  ['t_' + 'a'.repeat(24), '这个问题怎么排查？给出适用条件和来源。'],
])('fills the displayed placeholder on Enter in an empty draft (topic %s)', async (focus_topic, prompt) => {
  const { transport, input } = setup({ source: 'local', days: 7, focus_topic })
  input.focus()
  expect(input.placeholder).toBe(prompt)
  expect(input.value).toBe('')

  fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
  fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
  expect(input.value).toBe('')
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(input.value).toBe(prompt)
  expect(document.activeElement).toBe(input)
  fireEvent.keyDown(input, { key: 'Enter', repeat: true })
  expect(transport).not.toHaveBeenCalled()

  fireEvent.keyDown(input, { key: 'Enter' })
  expect(transport).toHaveBeenCalledTimes(1)
  expect(transport.mock.calls[0][1].message).toBe(prompt)
  await screen.findByText('示例答复')
})

it('completes a typed keyword in place and only sends on a fresh Enter', async () => {
  const { transport, input } = setup({ source: 'local', days: 7 })
  input.focus()
  fireEvent.change(input, { target: { value: '话术' } })
  expect(screen.getByRole('button', { name: /客服通常怎么回复场景未触发的问题.*Enter 补全/ })).toBeTruthy()

  fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
  fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
  expect(input.value).toBe('话术')
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(input.value).toBe('客服通常怎么回复场景未触发的问题？')
  expect(document.activeElement).toBe(input)
  expect(screen.queryByText('Enter 补全')).toBeNull()
  fireEvent.keyDown(input, { key: 'Enter', repeat: true })
  expect(transport).not.toHaveBeenCalled()

  fireEvent.change(input, { target: { value: '补充平台范围：' + input.value } })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(transport).toHaveBeenCalledTimes(1)
  expect(transport.mock.calls[0][1].message).toBe('补充平台范围：客服通常怎么回复场景未触发的问题？')
  await screen.findByText('示例答复')
  fireEvent.change(input, { target: { value: '高频' } })
  expect(screen.getByRole('button', { name: /当前范围的高频咨询有哪些.*Enter 补全/ })).toBeTruthy()
})

it('uses topic-specific completions and lets Escape dismiss a suggestion for the current draft', async () => {
  const { transport, input } = setup({ source: 'local', days: 7, focus_topic: 't_' + 'a'.repeat(24) })
  fireEvent.change(input, { target: { value: '这个问题' } })
  expect(screen.queryByText('Enter 补全')).toBeNull()

  fireEvent.change(input, { target: { value: '这个问题怎么' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(input.value).toBe('这个问题怎么排查？给出适用条件和来源。')
  expect(transport).not.toHaveBeenCalled()

  fireEvent.change(input, { target: { value: '排查' } })
  expect(screen.getByRole('button', { name: /这个问题怎么排查.*Enter 补全/ })).toBeTruthy()
  fireEvent.keyDown(input, { key: 'Escape' })
  expect(screen.queryByText('Enter 补全')).toBeNull()
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(transport.mock.calls[0][1].message).toBe('排查')
  await screen.findByText('示例答复')
})

it('offers analysis examples before and during a conversation and shows the actual read tools', async () => {
  const { transport, input } = setup({ source: 'local', days: 7 }, {
    answer_mode: 'computed',
    tools: [{ name: 'daily_trend', label: '逐日计算咨询量与峰值' }],
  })
  const welcome = within(screen.getByRole('group', { name: '常用提问' }))
  expect(welcome.getByRole('button', { name: '最近 7 天的 VOC 复盘' })).toBeTruthy()
  fireEvent.click(welcome.getByRole('button', { name: '按天看咨询量和峰值' }))
  expect(input.value).toBe('按天看咨询量和峰值')
  expect(transport).not.toHaveBeenCalled()

  fireEvent.keyDown(input, { key: 'Enter' })
  await screen.findByText('示例答复')
  fireEvent.click(screen.getByText('查询范围与模型'))
  expect(screen.getByText('逐日计算咨询量与峰值')).toBeTruthy()
  expect(screen.getByText('按数据直接统计')).toBeTruthy()
  expect(screen.getByText('配置模型')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '提问示例' }))
  const examples = within(screen.getByRole('dialog', { name: '提问示例' }))
  fireEvent.click(examples.getByRole('button', { name: '按客服看接待分布' }))
  expect(input.value).toBe('按客服看接待分布')
  expect(transport).toHaveBeenCalledTimes(1)
})

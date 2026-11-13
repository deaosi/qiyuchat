import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ApiFilters } from '@/lib/api'
import { createChatStore, type ChatAnswer } from '@/lib/voc-chat'
import { VocChatWorkspace } from './voc-chat-workspace'

const useApi = vi.hoisted(() => vi.fn(() => ({ data: null, loading: false, error: null })))
vi.mock('@/hooks/use-api', () => ({ useApi }))

afterEach(() => { cleanup(); useApi.mockClear() })

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

it('submits the guided evidence question and lists only the queries returned for that answer', async () => {
  const question = '找出当前范围最常见的一类问题，查看具体问法、已核验 SOP 和真实会话，并指出还缺哪些证据。'
  const { transport, input } = setup({ source: 'local', days: 7 })
  let finish!: (answer: ChatAnswer) => void
  transport.mockReturnValueOnce(new Promise<ChatAnswer>(resolve => { finish = resolve }))

  const welcome = within(screen.getByRole('group', { name: '常用提问' }))
  fireEvent.click(welcome.getByRole('button', { name: question }))
  expect(input.value).toBe(question)
  expect(transport).not.toHaveBeenCalled()
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(transport.mock.calls[0][1].message).toBe(question)
  expect(screen.getByText('正在查询数据并整理回答…')).toBeTruthy()

  await act(async () => finish({ answer: '已查到问法和一条原会话。', answer_mode: 'grounded_selection',
    citations: [], scope: { label: '近 7 天' }, model: 'test',
    tools: [{ name: 'rank', label: '统计高频问题' }, { name: 'cases', label: '查看相关会话' }],
  }))
  fireEvent.click(screen.getByText('查询范围与模型'))
  expect(screen.getByText('模型选取重点，按原始依据呈现')).toBeTruthy()
  const queries = within(screen.getByRole('list', { name: '本次已执行的查询' }))
  expect(queries.getAllByRole('listitem').map(item => item.textContent)).toEqual(['统计高频问题', '查看相关会话'])
})

it('shows the original question, reply and feedback with an event in the answer scope', async () => {
  const { input } = setup({ source: 'local', days: 7, group: '当前群' }, {
    citations: [{ kind: 'topic', id: 't_held', title: '保留主题依据' }],
    examples: [{
      citation: { kind: 'event', id: 'e_case', title: '商品权限排查' },
      question: '为什么配置没有触发？', reply: '打开子账号商品权限后重新测试。',
      time: '2026-11-10 13:24:55', outcome: '重新测试已经触发。',
    }],
    scope: { label: '案例查询范围', start: '2026-11-03', end: '2026-11-10', query_filters: { group: '案例群' } },
  })
  fireEvent.change(input, { target: { value: '给一个已核验案例' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  const example = within(await screen.findByRole('region', { name: '实际案例' }))
  expect(example.getByText('商家原话')).toBeTruthy()
  expect(example.getByText('为什么配置没有触发？')).toBeTruthy()
  expect(example.getByText('客服原话')).toBeTruthy()
  expect(example.getByText('打开子账号商品权限后重新测试。')).toBeTruthy()
  expect(example.getByText('重新测试已经触发。')).toBeTruthy()
  expect(example.getByText('2026-11-10 13:24:55')).toBeTruthy()
  expect(screen.getByText('查看依据（1）')).toBeTruthy()
  expect(screen.queryByRole('dialog', { name: '回答依据' })).toBeNull()
  fireEvent.click(example.getByRole('button', { name: '查看原会话' }))
  expect(screen.getByRole('dialog', { name: '回答依据' })).toBeTruthy()
  expect(useApi).toHaveBeenCalledWith('voc2/event',
    expect.objectContaining({ source: 'local', days: 0, start: '2026-11-03', end: '2026-11-10', group: '案例群', id: 'e_case' }), 0, true)
})

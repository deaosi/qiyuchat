import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ApiFilters } from '@/lib/api'
import { createChatStore, type ChatAnswer } from '@/lib/voc-chat'
import { VocChatWorkspace } from './voc-chat-workspace'

vi.mock('@/hooks/use-api', () => ({
  useApi: () => ({ data: null, loading: false, error: null }),
}))

afterEach(cleanup)

function setup(filters: ApiFilters) {
  const answer: ChatAnswer = { answer: '示例答复', citations: [], scope: { label: '近 7 天' }, model: 'test', tools: [] }
  const transport = vi.fn().mockResolvedValue(answer)
  const store = createChatStore({ transport })
  render(<VocChatWorkspace filters={filters} onScopeChange={vi.fn()} store={store} />)
  return { transport, input: screen.getByRole<HTMLTextAreaElement>('textbox', { name: '向语海Voc提问' }) }
}

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

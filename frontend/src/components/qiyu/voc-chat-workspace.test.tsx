import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

async function openPresets() {
  const trigger = screen.getByRole('button', { name: '预设话术' })
  trigger.focus()
  fireEvent.keyDown(trigger, { key: 'Enter' })
  const items = await screen.findAllByRole('menuitem')
  await waitFor(() => expect(document.activeElement).toBe(items[0]))
  return items
}

it('selects a preset with the keyboard, focuses the draft, and only sends on a fresh Enter', async () => {
  const { transport, input } = setup({ source: 'local', days: 7 })
  const [, replyPrompt] = await openPresets()
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' })
  await waitFor(() => expect(document.activeElement).toBe(replyPrompt))
  fireEvent.keyDown(replyPrompt, { key: 'Enter' })

  await waitFor(() => expect(document.activeElement).toBe(input))
  expect(input.value).toBe('客服通常怎么回复场景未触发的问题？')
  fireEvent.keyDown(input, { key: 'Enter', repeat: true })
  fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
  fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
  expect(transport).not.toHaveBeenCalled()

  fireEvent.change(input, { target: { value: '补充平台范围：' + input.value } })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(transport).toHaveBeenCalledTimes(1)
  expect(transport.mock.calls[0][1].message).toBe('补充平台范围：客服通常怎么回复场景未触发的问题？')
  await screen.findByText('示例答复')
  expect(screen.getByRole('button', { name: '预设话术' })).toBeTruthy()
})

it('offers topic-specific presets without replacing an existing draft', async () => {
  const { transport, input } = setup({ source: 'local', days: 7, focus_topic: 't_' + 'a'.repeat(24) })
  fireEvent.change(input, { target: { value: '只看抖音的咨询。' } })
  const [diagnose] = await openPresets()
  fireEvent.keyDown(diagnose, { key: 'Enter' })

  await waitFor(() => expect(document.activeElement).toBe(input))
  expect(input.value).toBe('只看抖音的咨询。\n这个问题怎么排查？给出适用条件和来源。')
  expect(transport).not.toHaveBeenCalled()
})

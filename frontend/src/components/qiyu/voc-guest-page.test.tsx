import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { Evidence } from './voc-next-workspace'
import { VocGuestPage, type GuestPayload } from './voc-guest-page'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const day = '2026-11-10'
const time = `${day} 12:34:56`
const quote = '商品状态设为在售'

function evidence(index: number, question: string): Evidence {
  return {
    id: `guest-case-${index}`, event_id: `guest-case-${index}`, qa_id: index + 1, question, answer: '',
    asked_at: time, staff_source_verified: false, claims: [],
  }
}

const first = evidence(0, '为什么配置没有触发？')
first.staff_source_verified = true
first.workbench = {
  commerce_settings: [{
    source: 'question', role: 'user', alignment: 'exact', quote, start: 0, end: quote.length,
    source_refs: [{ message_id: 'guest-message-1', start: 0, end: quote.length }],
  }],
  scenario_conditions: [], actual: [],
}
const second = evidence(1, '另一场会话的咨询')

const payload: GuestPayload = {
  entry: { kind: 'topics', title: '配置未触发', summary: '', detail: '', start: day, end: day },
  cases: [
    { title: first.question, category: '', time, sections: [], scope: 'context', messages: [
      { id: 'guest-message-0', role: 'user', text: '之前的客户消息', time, anchor: true },
      { id: 'guest-message-1', role: 'user', text: quote, time },
      { id: 'guest-message-2', role: 'staff', text: '请重新测试命中条件。', time },
    ] },
    { title: second.question, category: '', time, sections: [], scope: 'pair', messages: [
      { id: 'guest-message-0', role: 'user', text: second.question, time, anchor: true },
    ] },
  ],
  total_cases: 2,
  topic_detail: {
    topic: {
      id: 'guest-topic', theme: '场景配置 / 未触发', question: '配置未触发', consultation_count: 2,
      event_count: 2, customer_count: 2, session_count: 2, mention_count: 2, last_seen: time, samples: [],
    },
    events: [second, first],
    workbench: {
      comparison: { current: { start: day, end: day, source_pairs: 10, processed_pairs: 8 },
        previous: null, comparable: false, last_synced_at: time, note: '' },
      knowledge: [], gaps: [{ code: 'no_staff_reply', count: 1 }],
    },
  },
}

it('shows the regular topic detail and switches to another scoped case without admin requests', () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  render(<VocGuestPage {...payload} />)
  expect(screen.getByRole('heading', { name: '问题与原句依据' })).toBeTruthy()
  expect(screen.getByText('2 次咨询 · 已整理 8 / 10 条问答')).toBeTruthy()
  expect(screen.getByText('处理判断 · 代表案例')).toBeTruthy()
  expect(screen.getByText('缺少人工客服回复 1 次咨询')).toBeTruthy()
  expect(screen.queryByRole('button', { name: '追问这个问题' })).toBeNull()
  expect(screen.queryByText(/问答 [12] ·/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '查看处理判断' }))
  const card = screen.getByText(second.question).closest('article')!
  fireEvent.click(within(card).getByRole('button', { name: '查看会话上下文' }))
  const sheet = screen.getByRole('dialog', { name: '问答与相关会话上下文' })
  expect(within(sheet).getByText(second.question)).toBeTruthy()
  expect(within(sheet).queryByText(quote)).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})

it('opens the same chat drawer at the exact shared source when locating a quotation', () => {
  render(<VocGuestPage {...payload} />)
  fireEvent.click(screen.getByRole('button', { name: '定位原话' }))
  const sheet = screen.getByRole('dialog')
  const target = within(sheet).getByText(quote).closest('li')!
  const before = within(sheet).getByText('之前的客户消息').closest('li')!
  const reply = within(sheet).getByText('请重新测试命中条件。').closest('li')!
  expect(target.hasAttribute('data-evidence-anchor')).toBe(true)
  expect(before.hasAttribute('data-evidence-anchor')).toBe(false)
  expect(target.getAttribute('data-side')).toBe('left')
  expect(reply.getAttribute('data-side')).toBe('right')
})

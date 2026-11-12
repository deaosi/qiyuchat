import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { Evidence, TopicDetail } from './voc-next-workspace'
import { TopicWorkbench } from './voc-topic-detail'
import { OriginalExchange } from './voc-topic-evidence'

afterEach(cleanup)

it('shows the original question and verified reply in the main case, without treating unverified text as a staff reply', () => {
  const verified: Evidence = {
    id: 'first', event_id: 'first', qa_id: 11, question: '完整客户问题：同步后商品没有显示。', question_quote: '商品没有显示',
    answer: '客服原文：请先检查授权，然后重新同步。', staff_source_verified: true, claims: [], question_message_ids: ['question-11'],
  }
  const unverified: Evidence = { ...verified, id: 'second', event_id: 'second', qa_id: 12,
    question: '第二个客户原问', answer: '身份不明的回复内容', staff_source_verified: false, question_message_ids: ['question-12'] }
  const absent: Evidence = { ...verified, id: 'third', event_id: 'third', qa_id: 13,
    question: '第三个客户原问', answer: '', staff_source_verified: false, claims: [] }
  const detail: TopicDetail = {
    topic: { id: 'topic', theme: '商品同步', question: '商品为什么没有显示？', consultation_count: 3,
      event_count: 3, customer_count: 1, session_count: 3, mention_count: 3, last_seen: '', samples: [] },
    events: [verified, unverified, absent],
    workbench: { comparison: { current: { start: '', end: '' }, previous: null, comparable: false, note: '', last_synced_at: '' }, knowledge: [], gaps: [] },
  }
  const open = vi.fn()
  render(<TopicWorkbench detail={detail} onOpenEvidence={open} />)

  const main = screen.getByText(verified.question).closest('article')!
  expect(within(main).getByText(verified.answer)).toBeTruthy()
  expect(within(main).getByText('客户问题原文')).toBeTruthy()
  fireEvent.click(within(main).getByRole('button', { name: '跳转到会话上下文' }))
  expect(open).toHaveBeenCalledWith(verified, 'question-11')
  expect(screen.queryByText(unverified.answer)).toBeNull()
  expect(screen.getByText('回复来源尚未核验，请查看会话上下文确认。')).toBeTruthy()
  expect(screen.getByText('当前问答片段未提取到客服回复，请查看会话上下文。')).toBeTruthy()
})

it('uses verified staff claims when the source answer is empty, excluding non-staff claims', () => {
  const item: Evidence = { id: 'claim', event_id: 'claim', question: '客户提问', answer: '', staff_source_verified: true,
    claims: [
      { role: 'staff', quote: '客服已核验原句', start: 0, end: 7, source_refs: [] },
      { role: 'user', quote: '客户后续反馈', start: 0, end: 6, source_refs: [] },
    ] }
  render(<OriginalExchange item={item} />)
  expect(screen.getByText('客服已核验原句')).toBeTruthy()
  expect(screen.queryByText('客户后续反馈')).toBeNull()
})

it('prefers a case with a verified nonempty reply in the frequency view', () => {
  const empty: Evidence = { id: 'empty', event_id: 'empty', qa_id: 1, question: '较早问题', answer: '', staff_source_verified: true, claims: [] }
  const answered: Evidence = { ...empty, id: 'answered', event_id: 'answered', qa_id: 2, question: '有回复的问题', answer: '真实客服回复' }
  const detail: TopicDetail = {
    topic: { id: 'topic', theme: '主题', question: '问题', event_count: 2, customer_count: 1, session_count: 2, mention_count: 2, last_seen: '', samples: [] },
    events: [empty, answered], workbench: { comparison: { current: { start: '', end: '' }, previous: null, comparable: false, note: '', last_synced_at: '' }, knowledge: [], gaps: [] },
  }
  render(<TopicWorkbench detail={detail} onOpenEvidence={vi.fn()} exchangeOnly />)
  const representative = screen.getByText('有回复的问题').closest('article')!
  expect(within(representative).getByText('真实客服回复')).toBeTruthy()
  expect(screen.getByText('代表问答')).toBeTruthy()
  expect(screen.queryByText(/处理判断|待补证据/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '查看问答' }))
  expect(screen.getByText('当前问答')).toBeTruthy()
})

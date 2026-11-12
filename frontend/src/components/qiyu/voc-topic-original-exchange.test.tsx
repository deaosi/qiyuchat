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
  fireEvent.click(within(main).getByRole('button', { name: '查看会话上下文' }))
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
  const empty: Evidence = { id: 'empty', event_id: 'empty', qa_id: 1, question: '较早问题', question_quote: '较早问题', answer: '', staff_source_verified: true, claims: [] }
  const answered: Evidence = { ...empty, id: 'answered', event_id: 'answered', qa_id: 2, question: '有回复的问题', question_quote: '有回复的问题', source_kind: 'source-anchored', answer: '真实客服回复' }
  const legacyAnswered: Evidence = { ...answered, id: 'legacy', event_id: 'legacy', qa_id: 3, source_kind: 'legacy-reference', question_quote: '未确认发言人的片段' }
  const detail: TopicDetail = {
    topic: { id: 'topic', theme: '主题', question: '问题', event_count: 2, customer_count: 1, session_count: 2, mention_count: 2, last_seen: '', samples: [] },
    events: [empty, legacyAnswered, answered], workbench: { comparison: { current: { start: '', end: '' }, previous: null, comparable: false, note: '', last_synced_at: '' }, knowledge: [], gaps: [] },
  }
  render(<TopicWorkbench detail={detail} onOpenEvidence={vi.fn()} exchangeOnly />)
  const representative = screen.getByText('有回复的问题').closest('article')!
  expect(within(representative).getByText('真实客服回复')).toBeTruthy()
  expect(screen.getByText('代表问答')).toBeTruthy()
  expect(screen.queryByText(/处理判断|待补证据/)).toBeNull()
  fireEvent.click(screen.getAllByRole('button', { name: '查看问答' })[0])
  expect(screen.getByText('当前问答')).toBeTruthy()
})

it('deduplicates valid QA identities, prefers verified replies and avoids mixed-role full question text', () => {
  const base: Evidence = { id: 'a', event_id: 'a', qa_id: 91, question: '客户：提问\n客服：请稍等\n客户：补充说明',
    question_quote: '提问', answer: '来源未核验', staff_source_verified: false, claims: [] }
  const duplicate: Evidence = { ...base, id: 'b', event_id: 'b', qa_id: '91', answer: '已核验的回复', staff_source_verified: true }
  const duplicateAgain: Evidence = { ...base, id: 'c', event_id: 'c', qa_id: 91, question_quote: '补充说明' }
  const noQuote: Evidence = { ...base, id: 'd', event_id: 'd', qa_id: 92, question_quote: '', answer: '' }
  const detail: TopicDetail = { topic: { id: 'topic', theme: '主题', question: '归纳问题', event_count: 4, customer_count: 1, session_count: 2, mention_count: 4, last_seen: '', samples: [] },
    events: [base, duplicate, duplicateAgain, noQuote], workbench: { comparison: { current: { start: '', end: '' }, previous: null, comparable: false, note: '', last_synced_at: '' }, knowledge: [], gaps: [] } }
  const open = vi.fn()
  render(<TopicWorkbench detail={detail} onOpenEvidence={open} exchangeOnly />)
  const main = screen.getByText('提问').closest('article')!
  expect(within(main).getByText('已核验的回复')).toBeTruthy()
  expect(screen.queryByText(base.question)).toBeNull()
  expect(screen.queryByText('补充说明')).toBeNull()
  expect(within(main).getByText('会话片段（发言人待核对）')).toBeTruthy()
  expect(screen.getByText('未提取到当前会话片段，请查看会话上下文。')).toBeTruthy()
  expect(screen.getAllByRole('button', { name: '查看会话上下文' })).toHaveLength(2)
  fireEvent.click(within(main).getByRole('button', { name: '查看会话上下文' }))
  expect(open).toHaveBeenCalledWith(duplicate, undefined)
})

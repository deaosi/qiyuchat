import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useApi } from '@/hooks/use-api'
import { VocKnowledgeWorkspace, type Knowledge } from './voc-next-workspace'

vi.mock('@/hooks/use-api', () => ({ useApi: vi.fn() }))
afterEach(cleanup)

it('finds source records by scope and steps and filters reusable knowledge by evidence level', () => {
  const common: Omit<Knowledge, 'id' | 'question'> = {
    topic_id: 'topic-1', answer: '完成配置后保存。', scope: '店铺后台', steps: ['检查开关'], cautions: [],
    reply_patterns: [], evidence_count: 0, status: 'ready', version: 1, updated_at: '', evidence: [],
  }
  const items: Knowledge[] = [
    { ...common, id: 'verified', question: '开启商品匹配', knowledge_reuse: { level: 'verified', reason_codes: [] } },
    { ...common, id: 'guidance', question: '设置关键词', knowledge_reuse: { level: 'guidance', reason_codes: [] } },
    { ...common, id: 'reference', question: '历史配置答复', knowledge_reuse: { level: 'reference', reason_codes: [] },
      scope: 'ERP 管理员', steps: ['查看二次验收结果'] },
  ]
  vi.mocked(useApi).mockReturnValue({ data: { items, total: items.length }, loading: false, refreshing: false, error: null, retry: vi.fn() })
  render(<VocKnowledgeWorkspace filters={{ source: 'local' }} onOpenPair={vi.fn()} />)

  const search = screen.getByRole('searchbox', { name: '搜索知识' })
  expect(screen.queryByRole('heading', { name: '历史配置答复' })).toBeNull()
  fireEvent.change(search, { target: { value: 'ｅｒｐ 二次验收' } })
  expect(screen.getByRole('heading', { name: '历史配置答复' })).toBeTruthy()
  expect(screen.getByRole('status').textContent).toBe('找到 1 条知识')
  expect(screen.queryByRole('button', { name: /复制/ })).toBeNull()

  fireEvent.click(screen.getByRole('button', { name: '已核验指引' }))
  expect(screen.getByText('未找到符合条件的知识')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '清除筛选' }))
  expect(screen.getByRole('heading', { name: '开启商品匹配' })).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: '已核验指引' }))
  expect(screen.getByRole('heading', { name: '设置关键词' })).toBeTruthy()
  expect(screen.getByRole('button', { name: '复制指引' })).toBeTruthy()
  expect(screen.queryByRole('heading', { name: '开启商品匹配' })).toBeNull()
})

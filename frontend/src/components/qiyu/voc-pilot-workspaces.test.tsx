import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { VocCasePath, VocIssuePilot, VocPilotEvidence, VocPlaybook } from '@/lib/api'
import { VocIssuePilotResults, VocPlaybookResults } from './voc-pilot-workspaces'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); history.replaceState(null, '', '/') })

const source: VocPilotEvidence = { qa_id: 42, message_id: 'source-3', quote: '商家反馈授权后仍然无法拉取商品。', ts: 1794441600000 }
const issueId = 'issue_' + 'a'.repeat(24)

it('shows an unfamiliar product issue and opens its exact conversation anchor', () => {
  const data: VocIssuePilot = {
    items: [{
      id: issueId, title: '商品授权后拉取失败', family: 'other', platform: '测试平台', module: '商品授权',
      condition: '子账号', scope_status: 'needs_context', evidence_status: 'reported',
      impact: { consultations: 3, sessions: 2, merchants: 1, unidentified_sessions: 1 },
      next: { owner: 'support', action: '核对授权回执。', missing: ['root_cause'] },
      cases: [{ ...source, anchored: true, has_reply: false }],
    }],
    total: 1, coverage: { source_events: 25, matched_consultations: 6, classified_consultations: 3, needs_context: 3 },
  }
  const open = vi.fn()
  render(<VocIssuePilotResults data={data} onOpenPair={open} />)
  expect(screen.getByText('待补上下文 3 次')).toBeTruthy()
  expect(screen.getByText('范围待补')).toBeTruthy()
  const search = screen.getByRole('searchbox', { name: '搜索产品问题' })
  fireEvent.change(search, { target: { value: '核对授权回执' } })
  expect(screen.queryByRole('button', { name: /商品授权后拉取失败/ })).toBeNull()
  fireEvent.change(search, { target: { value: '子账号 无法拉取' } })
  fireEvent.click(screen.getByRole('button', { name: /商品授权后拉取失败/ }))
  const detail = within(screen.getByRole('dialog', { name: '商品授权后拉取失败' }))
  expect(detail.getByText('识别 1 家商家')).toBeTruthy()
  expect(detail.queryByText('核对授权回执。')).toBeNull()
  expect(detail.getByText('原因确认', { exact: false })).toBeTruthy()
  expect(detail.getByRole('link', { name: '查看处理手册' }).getAttribute('href')).toBe(`#voc-next?issue=${issueId}`)
  fireEvent.click(detail.getByRole('button', { name: '定位原话' }))
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '42' }), 'source-3')
})

it('shows the actual authorization consultation and exact message instead of a stale generic guide', () => {
  const question = { ...source, quote: '怎么授权啊' }
  const playbook: VocPlaybook = {
    id: 'playbook-1', issue_id: issueId, title: '授权：绑定与授权', family: 'configuration', platform: '', module: '绑定与授权', condition: '',
    status: 'investigation', checked_cases: 0, branches: [],
    triage: [{ check: '核对店铺、账号与授权角色。', when_yes: '检查账户权限。', when_no: '补齐账号。' }],
    missing: ['使用的平台', 'case_evidence'], evidence: [{ ...source, quote: '旧版会话依据' }],
    cases: [{ ...question, anchored: true, has_reply: false }, {
      ...source, qa_id: 61, message_id: '', anchored: false, has_reply: false,
      details: { goal: '拉取新商品', observed: '商品列表未更新', checked: '重新授权后刷新过列表' },
    }],
    impact: { consultations: 3, sessions: 2, merchants: 1, unidentified_sessions: 1 },
  }
  const open = vi.fn()
  render(<VocPlaybookResults items={[playbook]} onOpenPair={open} />)
  const search = screen.getByRole('searchbox', { name: '搜索处理手册' })
  fireEvent.change(search, { target: { value: '授权角色' } })
  expect(screen.queryByRole('button', { name: /怎么授权啊/ })).toBeNull()
  fireEvent.change(search, { target: { value: '怎么授权啊 刷新过列表' } })
  fireEvent.click(screen.getByRole('button', { name: /怎么授权啊.*仅有咨询记录/ }))

  const detail = within(screen.getByRole('dialog', { name: '咨询详情' }))
  expect(detail.getByText('仅有咨询记录')).toBeTruthy()
  expect(detail.getByText('客户咨询摘录')).toBeTruthy()
  expect(detail.queryByText('0 个关联处理案例')).toBeNull()
  expect(detail.getByText('3 次咨询')).toBeTruthy()
  expect(detail.getByText('2 个会话')).toBeTruthy()
  expect(detail.getByText('识别 1 家商家')).toBeTruthy()
  expect(detail.getByText('怎么授权啊')).toBeTruthy()
  expect(detail.getByText('拉取新商品')).toBeTruthy()
  expect(detail.getByText('商品列表未更新')).toBeTruthy()
  expect(detail.getByText('重新授权后刷新过列表')).toBeTruthy()
  expect(detail.getByText('使用的平台、完整处理记录')).toBeTruthy()
  expect(detail.queryByText(/授权角色|排查决策|检查账户权限|旧版会话依据/)).toBeNull()
  expect(detail.queryByRole('button', { name: '复制处理步骤' })).toBeNull()
  fireEvent.click(detail.getByRole('button', { name: '定位原话' }))
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '42', question: question.quote }), 'source-3')
  fireEvent.click(detail.getByRole('button', { name: '查看会话' }))
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '61' }), undefined)
})

it('shows the sourced operation ahead of pending consultations and keeps the actual outcome open', () => {
  const problem = { ...source, quote: '拼多多发出「稍等」以后没有待办。' }
  const evidence = { ...source, message_id: 'source-4', quote: '我这边配置为知识加入待办。', ts: source.ts + 1000 }
  const criterion = { ...source, message_id: 'source-5', quote: '想让机器人回复稍等时也能加入待办。', ts: source.ts + 2000 }
  const path: VocCasePath = { id: 'case-1', qa_id: source.qa_id, problem, platform: '拼多多', outcome: 'not_recorded', criterion,
    steps: [{ actor: 'staff', state: 'reported', action: '配置为知识加入待办', evidence }] }
  const pending: VocPlaybook = { id: 'pending', issue_id: issueId, title: '商品授权待确认', family: 'configuration', platform: '', module: '', condition: '',
    status: 'investigation', checked_cases: 0, triage: [], branches: [], missing: [], evidence: [] }
  const open = vi.fn()
  render(<VocPlaybookResults items={[pending]} casePaths={[path]} onOpenPair={open} />)

  const caseButton = screen.getByRole('button', { name: /拼多多发出.*配置为知识加入待办/ })
  const pendingButton = screen.getByRole('button', { name: /商品授权待确认/ })
  expect(caseButton.compareDocumentPosition(pendingButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.getByRole('heading', { name: '已有处理记录的案例' })).toBeTruthy()
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索处理手册' }), { target: { value: '拼多多 配置为知识加入待办' } })
  expect(screen.queryByRole('button', { name: /商品授权待确认/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /拼多多发出.*配置为知识加入待办/ }))

  const detail = within(screen.getByRole('dialog', { name: '案例处理记录' }))
  expect(detail.getByText('客服 · 说明处理情况')).toBeTruthy()
  expect(detail.getByText('配置为知识加入待办')).toBeTruthy()
  expect(detail.getByRole('heading', { name: '客户期望' })).toBeTruthy()
  expect(detail.getByText(criterion.quote)).toBeTruthy()
  expect(detail.getByText('未记录复测结果')).toBeTruthy()
  expect(detail.queryByText('效果已验证')).toBeNull()
  expect(detail.queryByRole('button', { name: '复制处理步骤' })).toBeNull()

  const original = detail.getByText('查看客服原话').closest('details')!
  expect(within(original).getByText(evidence.quote)).toBeTruthy()
  fireEvent.click(original.querySelector('summary')!)
  fireEvent.click(within(original).getByRole('button', { name: '定位原话' }))
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '42' }), 'source-4')
})

it('preserves the playbook deep link and copies only branches with sourced steps and retest', async () => {
  const playbook: VocPlaybook = {
    id: 'playbook-1', issue_id: issueId, title: '检查商品授权', family: 'platform_limit', platform: '', module: '商品授权', condition: '子账号',
    status: 'investigation', checked_cases: 2,
    triage: [{ check: '是否有授权回执？', when_yes: '继续核对平台。', when_no: '请商家提供回执。' }],
    branches: [{ condition: '已有回执', condition_evidence: [source], steps: [{ text: '核对回执与子账号是否一致。', evidence: [source] }],
      retest: '再次拉取商品。', retest_evidence: [{ ...source, message_id: 'retest-4' }], escalation: '转交平台核对。' }, {
      condition: '没有回执', steps: [{ text: '清空店铺授权。', evidence: [source] }], retest: '重新授权。', escalation: '',
    }],
    missing: ['verified_outcome'], evidence: [source],
  }
  const open = vi.fn()
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  history.replaceState(null, '', `#voc-next?issue=${issueId}`)
  const { rerender } = render(<VocPlaybookResults items={[]} onOpenPair={open} />)
  rerender(<VocPlaybookResults items={[playbook]} onOpenPair={open} />)
  let detail = within(screen.getByRole('dialog', { name: '咨询详情' }))
  expect(detail.queryByText('继续核对平台。', { exact: false })).toBeNull()
  expect(detail.queryByRole('button', { name: '复制处理步骤' })).toBeNull()

  rerender(<VocPlaybookResults items={[{ ...playbook, status: 'guidance' }]} onOpenPair={open} />)
  detail = within(screen.getByRole('dialog', { name: '检查商品授权' }))
  expect(detail.getByText('2 个关联处理案例')).toBeTruthy()
  expect(detail.queryByText(/清空店铺授权|转交平台核对/)).toBeNull()
  const evidence = detail.getByText('步骤依据').closest('details')!
  fireEvent.click(evidence.querySelector('summary')!)
  fireEvent.click(within(evidence).getByRole('button', { name: '定位原话' }))
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '42' }), 'source-3')
  fireEvent.click(detail.getByRole('button', { name: '复制处理步骤' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining('核对回执与子账号是否一致。')))
  expect(writeText).not.toHaveBeenCalledWith(expect.stringContaining('请商家提供回执。'))
})

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { VocIssuePilot, VocPilotEvidence, VocPlaybook } from '@/lib/api'
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
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索产品问题' }), { target: { value: '子账号 回执' } })
  fireEvent.click(screen.getByRole('button', { name: /商品授权后拉取失败/ }))
  const detail = within(screen.getByRole('dialog', { name: '商品授权后拉取失败' }))
  expect(detail.getByText('识别 1 家商家')).toBeTruthy()
  expect(detail.getByText('原因确认', { exact: false })).toBeTruthy()
  expect(detail.getByRole('link', { name: '查看处理手册' }).getAttribute('href')).toBe(`#voc-next?issue=${issueId}`)
  fireEvent.click(detail.getByRole('button', { name: '定位原话' }))
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '42' }), 'source-3')
})

it('keeps investigation-only advice out of the copy action and preserves branch evidence', async () => {
  const playbook: VocPlaybook = {
    id: 'playbook-1', issue_id: issueId, title: '检查商品授权', family: 'platform_limit', platform: '', module: '商品授权', condition: '子账号',
    status: 'investigation', checked_cases: 2,
    triage: [{ check: '是否有授权回执？', when_yes: '继续核对平台。', when_no: '请商家提供回执。' }],
    branches: [{ condition: '已有回执', steps: [{ text: '核对回执与子账号是否一致。', evidence: [source] }], retest: '再次拉取商品。', escalation: '转交平台核对。' }],
    missing: ['verified_outcome'], evidence: [source],
  }
  const open = vi.fn()
  const writeText = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  history.replaceState(null, '', `#voc-next?issue=${issueId}`)
  const { rerender } = render(<VocPlaybookResults items={[]} onOpenPair={open} />)
  rerender(<VocPlaybookResults items={[playbook]} onOpenPair={open} />)
  let detail = within(screen.getByRole('dialog', { name: '检查商品授权' }))
  expect(detail.getByText('继续核对平台。', { exact: false })).toBeTruthy()
  expect(detail.queryByRole('button', { name: '复制处理步骤' })).toBeNull()

  rerender(<VocPlaybookResults items={[{ ...playbook, status: 'guidance' }]} onOpenPair={open} />)
  detail = within(screen.getByRole('dialog', { name: '检查商品授权' }))
  const evidence = detail.getByText('步骤依据').closest('details')!
  fireEvent.click(evidence.querySelector('summary')!)
  fireEvent.click(within(evidence).getByRole('button', { name: '定位原话' }))
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: '42' }), 'source-3')
  fireEvent.click(detail.getByRole('button', { name: '复制处理步骤' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining('核对回执与子账号是否一致。')))
  expect(writeText).not.toHaveBeenCalledWith(expect.stringContaining('请商家提供回执。'))
})

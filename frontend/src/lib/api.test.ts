import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiGet, apiPost } from './api'
import { requestActivity } from './request-activity'

const filters = { source: 'local', id: '21' } as const
const staffMessage = {
  role: 'staff', content: '回复原文', ts: 1794358801000, time: '2026-11-11 09:00:01',
  source_message_id: 'official-staff-1', is_question: false, is_answer: false,
}

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => vi.unstubAllGlobals())

it('accepts a mapped category with one member only when its stable key and representative are valid', async () => {
  const workbench = { ...filters, workbench: 1 } as const
  const representative = `t_${'a'.repeat(24)}`
  const item = {
    id: representative, question: '为什么机器人不回复？', theme: '机器人回复异常', last_seen: '',
    event_count: 1, customer_count: 1, session_count: 1, mention_count: 1, samples: [],
    platforms: [], merchant_count: 1, unknown_merchant_sessions: 0,
    trend: { status: 'unknown', previous_consultation_count: null, delta: null },
    category_key: `t_${'b'.repeat(24)}`,
    members: [{ id: representative, question: '为什么机器人不回复？', theme: '机器人不回复', consultation_count: 1 }],
  }
  const comparison = { current: { start: '', end: '', source_pairs: 1, processed_pairs: 1 }, previous: null,
    comparable: false, note: '', last_synced_at: '' }
  const valid = { items: [item], total: 1, category_total: 1, comparison }
  const fetch = vi.fn().mockResolvedValueOnce(json(valid))
  for (const invalid of [
    { ...item, category_key: undefined }, { ...item, category_key: ' ' }, { ...item, members: [] },
    { ...item, members: [{ ...item.members[0], id: `t_${'c'.repeat(24)}` }] },
  ]) fetch.mockResolvedValueOnce(json({ ...valid, items: [invalid] }))
  vi.stubGlobal('fetch', fetch)
  await expect(apiGet('voc2/topics', workbench)).resolves.toEqual(valid)
  for (let index = 0; index < 4; index++) await expect(apiGet('voc2/topics', workbench)).rejects.toMatchObject({ status: 502 })
})

it('reads ranking metrics and job status together without weakening the job contract', async () => {
  const ranking = { items: [], total: 0, event_count: 23, category_total: 0, category_consultation_count: 0,
    status: { worker_ready: true, semantics: { ready: false, error: '语义模型尚未就绪' },
      latest_job: { id: 'job-1', status: 'running', total: 10, processed: 2, failed: 0, skipped: 0, usage_tokens: 32, error: '' } },
    comparison: { current: { start: '', end: '', source_pairs: 23, processed_pairs: 2 }, previous: null,
      comparable: false, note: '', last_synced_at: '' },
  }
  const malformed = { ...ranking, status: { ...ranking.status, latest_job: { ...ranking.status.latest_job, processed: '2' } } }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(ranking)).mockResolvedValueOnce(json(malformed)))
  await expect(apiGet('voc2/topics', { ...filters, workbench: 1 })).resolves.toEqual(ranking)
  await expect(apiGet('voc2/topics', { ...filters, workbench: 1 })).rejects.toMatchObject({ status: 502 })
})

describe('VOC knowledge reuse response', () => {
  const item = {
    id: 'knowledge-1', topic_id: 'topic-1', question: '如何启用规则？', answer: '在后台启用规则。', scope: '',
    steps: [], reply_patterns: [], cautions: [], evidence: [], status: 'ready',
  }

  it.each([
    undefined, { level: 'guidance', reason_codes: ['source_confirmed'] },
    { level: 'verified', reason_codes: [], evidence: [{ quote: '打开后成功触发', role: 'merchant', message_id: 13, ts: 1794358801000 }] },
  ])('accepts optional reuse metadata %#', async knowledge_reuse => {
    const data = { items: [{ ...item, knowledge_reuse }], total: 1 }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(data)))
    await expect(apiGet('voc2/knowledge', filters)).resolves.toEqual(JSON.parse(JSON.stringify(data)))
  })

  it.each([
    null, { level: 'approved', reason_codes: [] }, { level: ['guidance'], reason_codes: [] },
    { level: 'reference', reason_codes: [1] }, { level: 'verified' },
    { level: 'verified', reason_codes: [], evidence: {} },
    { level: 'verified', reason_codes: [], evidence: [{ quote: true, role: 'merchant' }] },
    { level: 'verified', reason_codes: [], evidence: [{ quote: '成功', role: 'merchant', ts: false }] },
  ])('rejects malformed reuse metadata %#', async knowledge_reuse => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ item: { ...item, knowledge_reuse } })))
    await expect(apiGet('voc2/knowledge', filters)).rejects.toMatchObject({ status: 502 })
  })
})

describe('VOC product evidence responses', () => {
  const anchor = { qa_id: 17, message_id: 'msg-7', quote: '平台侧出现授权提示。', ts: 1794358801000 }
  const shared = { id: 'item-1', title: '授权校验', family: 'other', platform: '', module: '', condition: '' }

  it('accepts a case without a message anchor but rejects it when declared anchored', async () => {
    const item = { ...shared, scope_status: 'needs_context', evidence_status: 'reported',
      impact: { consultations: 1, sessions: 1, merchants: 0, unidentified_sessions: 1 },
      next: { owner: '客服支持', action: '确认授权状态。', missing: ['适用条件'] },
      cases: [{ ...anchor, message_id: '', anchored: false, has_reply: false, details: { observed: '平台侧出现授权提示' } }],
    }
    const data = { items: [item], total: 1, coverage: { source_events: 1, matched_consultations: 1, classified_consultations: 1, needs_context: 0 } }
    const fetch = vi.fn().mockResolvedValueOnce(json(data)).mockResolvedValueOnce(json({ ...data, items: [{ ...item, cases: [{ ...item.cases[0], anchored: true }] }] }))
    vi.stubGlobal('fetch', fetch)
    await expect(apiGet('voc2/issue-pilot', filters)).resolves.toEqual(data)
    await expect(apiGet('voc2/issue-pilot', filters)).rejects.toMatchObject({ status: 502 })
  })

  it('validates grounded consultations, impact and conflicting source anchors on playbooks', async () => {
    const consultation = { ...anchor, anchored: true, has_reply: false, details: { goal: '启用商品授权', observed: '授权后商品列表为空' } }
    const item = { ...shared, issue_id: 'issue-1', status: 'investigation', checked_cases: 0,
      triage: [], branches: [], missing: ['case_evidence'], evidence: [], cases: [consultation],
      impact: { consultations: 1, sessions: 1, merchants: 0, unidentified_sessions: 1 },
      counterevidence: [{ ...anchor, qa_id: 24, message_id: 'msg-8' }],
    }
    const data = { items: [item], total: 1 }
    const fetch = vi.fn().mockResolvedValueOnce(json(data))
    for (const invalid of [
      { cases: [{ ...consultation, details: { checked: ['尝试授权'] } }] },
      { impact: { ...item.impact, sessions: -1 } },
      { counterevidence: [{ ...item.counterevidence[0], message_id: '' }] },
    ]) fetch.mockResolvedValueOnce(json({ ...data, items: [{ ...item, ...invalid }] }))
    vi.stubGlobal('fetch', fetch)
    await expect(apiGet('voc2/playbooks', filters)).resolves.toEqual(data)
    for (let i = 0; i < 3; i++) await expect(apiGet('voc2/playbooks', filters)).rejects.toMatchObject({ status: 502 })
  })

  it('accepts exact sourced case actions and rejects invalid anchors or unsupported success', async () => {
    const step = { actor: 'staff', state: 'reported', action: '配置为知识加入待办',
      evidence: { ...anchor, message_id: 'msg-8', quote: '我这边配置为知识加入待办。' } }
    const path = { id: 'path-1', qa_id: anchor.qa_id, problem: anchor, steps: [step], platform: '拼多多', outcome: 'not_recorded',
      criterion: { ...anchor, message_id: 'msg-9', quote: '希望发送后会出现待办。' } }
    const data = { items: [], total: 0, case_paths: [path] }
    const fetch = vi.fn().mockResolvedValueOnce(json(data))
    const invalidPaths = [
      { ...path, outcome: 'verified' },
      { ...path, qa_id: 22 },
      { ...path, criterion: { ...path.criterion, message_id: '' } },
      { ...path, steps: [{ ...step, action: '点击保存' }] },
      { ...path, steps: [{ ...step, evidence: { ...step.evidence, message_id: '' } }] },
    ]
    for (const invalid of invalidPaths) fetch.mockResolvedValueOnce(json({ ...data, case_paths: [invalid] }))
    vi.stubGlobal('fetch', fetch)
    await expect(apiGet('voc2/playbooks', filters)).resolves.toEqual(data)
    for (const _invalid of invalidPaths) await expect(apiGet('voc2/playbooks', filters)).rejects.toMatchObject({ status: 502 })
  })

  it('accepts model-discovered cases with sourced sections and no literal steps', async () => {
    const action = { kind: 'action', text: '客服协助将当前知识加入待办。',
      evidence: [{ ...anchor, message_id: 'msg-8', quote: '我这边配置为知识加入待办。' }] }
    const solution = { title: '拼多多待办触发排查', sections: [
      action,
      { ...action, kind: 'next_step', text: '保留回复，复测待办。' },
      { ...action, kind: 'verification', mode: 'expected', text: 'AI 回复且生成待办。' },
    ] }
    const path = { id: 'path-1', qa_id: anchor.qa_id, problem: anchor, steps: [], platform: '拼多多', outcome: 'not_recorded', solution }
    const data = { items: [], total: 0, case_paths: [path] }
    const invalidPaths = [
      { ...path, solution: undefined },
      { ...path, solution: { ...solution, sections: [{ ...action, kind: 'result' }] } },
      { ...path, solution: { ...solution, sections: [{ ...action, kind: 'verification', mode: 'assumed' }] } },
      { ...path, solution: { ...solution, sections: [{ ...action, kind: 'next_step', mode: 'observed' }] } },
      { ...path, solution: { ...solution, sections: [{ ...action, evidence: [] }] } },
      { ...path, solution: { ...solution, sections: [{ ...action, evidence: [{ ...anchor, message_id: '' }] }] } },
    ]
    const fetch = vi.fn().mockResolvedValueOnce(json(data))
    for (const invalid of invalidPaths) fetch.mockResolvedValueOnce(json({ ...data, case_paths: [invalid] }))
    vi.stubGlobal('fetch', fetch)
    await expect(apiGet('voc2/playbooks', filters)).resolves.toEqual(data)
    for (const _invalid of invalidPaths) await expect(apiGet('voc2/playbooks', filters)).rejects.toMatchObject({ status: 502 })
  })

  it('rejects a playbook when a branch points to invalid escalation evidence', async () => {
    const data = { items: [{ ...shared, issue_id: 'issue-1', status: 'guidance', checked_cases: 1,
      triage: [{ check: '核对权限', when_yes: '继续', when_no: '补齐' }],
      branches: [{ condition: '授权已启用', steps: [{ text: '重新加载。', evidence: [anchor] }],
        retest: '复测。', escalation: '转交支持。', escalation_evidence: [{ ...anchor, qa_id: 0 }] }],
      missing: [], evidence: [anchor],
    }], total: 1 }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(data)))
    await expect(apiGet('voc2/playbooks', filters)).rejects.toMatchObject({ status: 502 })
  })
})

describe('official reply lookup transport', () => {
  it('uses the existing POST authorization headers without registering foreground activity', async () => {
    let respond!: (response: Response) => void
    const fetch = vi.fn((_url: string, _request: RequestInit) => new Promise<Response>(resolve => { respond = resolve }))
    vi.stubGlobal('fetch', fetch)
    const signal = new AbortController().signal
    const result = apiPost('detail/replies', filters, undefined, undefined, signal)
    expect(requestActivity.getSnapshot()).toMatchObject({ foreground: 0, background: 1 })
    expect(fetch).toHaveBeenCalledWith('/api/detail/replies?source=local&id=21', expect.objectContaining({
      method: 'POST', credentials: 'same-origin', signal, body: new Uint8Array(),
    }))
    const request = fetch.mock.calls[0][1]
    expect(new Headers(request.headers).get('X-Local-Request')).toBe('1')
    const data = { status: 'found', messages: [{ ...staffMessage, sender_name: '客服联系人' }] }
    respond(json(data))
    await expect(result).resolves.toEqual(data)
    expect(requestActivity.getSnapshot()).toMatchObject({ foreground: 0, background: 0 })
  })

  it('accepts identified senders without any supplemental staff replies', async () => {
    const data = { status: 'no_reply', context_revision: 'same-context', messages: [], senders: [
      { context_seq: 7, sender_kind: 'fuke_staff', sender_name: '技术支持王小明' },
      { context_seq: 8, sender_kind: 'group_participant', sender_name: '小顾' },
    ] }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(data)))
    await expect(apiPost('detail/replies', filters)).resolves.toEqual(data)
  })

  it.each([
    { status: 'unknown', messages: [] },
    { status: 'found', messages: [] },
    { status: 'no_reply', messages: [staffMessage] },
    { status: 'found', messages: [{ ...staffMessage, role: 'user' }] },
    { status: 'found', messages: [{ ...staffMessage, ts: 0 }] },
    { status: 'found', messages: [{ ...staffMessage, is_answer: true }] },
    { status: 'found', messages: [{ ...staffMessage, sender_name: 123 }] },
    { status: 'found', messages: [staffMessage, staffMessage] },
    { status: 'no_reply', messages: [], senders: [{ context_seq: 7 }, { context_seq: 7 }] },
    { status: 'no_reply', messages: [], senders: [{ context_seq: 0.5 }] },
    { status: 'no_reply', messages: [], context_revision: 123 },
  ])('rejects inconsistent lookup payload %#', async data => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(data)))
    await expect(apiPost('detail/replies', filters)).rejects.toMatchObject({ status: 502 })
  })
})

describe('retained transcript sender metadata', () => {
  const visitorMessage = { ...staffMessage, role: 'user' }
  const detail = { pair: { qa_id: 21, question: '案例问题', answer: '' }, archived: false }

  it('accepts old and individually labeled messages in the same transcript', async () => {
    const data = { ...detail, messages: [visitorMessage,
      { ...visitorMessage, context_seq: 1, sender_kind: 'fuke_staff', sender_name: '技术支持王小明' },
      { ...visitorMessage, context_seq: 2, sender_kind: 'group_participant', sender_name: '小顾' },
      { ...visitorMessage, sender_kind: 'unidentified', sender_account: 'unknown-user' },
    ] }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(data)))
    await expect(apiGet('detail', filters)).resolves.toEqual(data)
  })

  it.each([{ sender_kind: 'staff' }, { sender_name: [] }, { sender_account: 123 }])('rejects malformed sender metadata %#', async metadata => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...detail, messages: [{ ...visitorMessage, ...metadata }] })))
    await expect(apiGet('detail', filters)).rejects.toMatchObject({ status: 502 })
  })
})

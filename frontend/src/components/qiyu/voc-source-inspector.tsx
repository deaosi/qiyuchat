import { useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { useApi } from '@/hooks/use-api'
import { normalizePair, type ApiFilters } from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'
import { chatScopeLabel, type ChatCitation } from '@/lib/voc-chat'
import { customerLabel } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { QADetail } from './qa-detail'
import { QueryPanel, type Evidence, type Knowledge, type TopicDetail } from './voc-next-workspace'

function qaIdentity(item: Evidence): number | null {
  if (typeof item.qa_id !== 'number' && !(typeof item.qa_id === 'string' && /^\d+$/.test(item.qa_id))) return null
  const value = Number(item.qa_id)
  return Number.isSafeInteger(value) && value > 0 ? value : null
}

function consultationTime(item: Evidence): string {
  const raw = item.asked_at
  const value = typeof raw === 'number' || typeof raw === 'string' && /^\d+$/.test(raw) ? Number(raw) : NaN
  if (Number.isFinite(value) && value > 0) {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).format(value).replaceAll('/', '-')
  }
  return item.asked_at_text || (typeof raw === 'string' && raw ? raw : '时间未知')
}

function distinctCases(evidence: Evidence[]): Evidence[] {
  const seen = new Set<number>()
  return evidence.filter(item => {
    const id = qaIdentity(item)
    if (id === null) return true
    if (seen.has(id)) return false
    seen.add(id)
    return true
  })
}

export function VocSourceInspector({ citation, filters }: { citation: ChatCitation; filters: ApiFilters }) {
  const scopeKey = JSON.stringify([citation.kind, citation.id, filters])
  const [opened, setOpened] = useState<{ key: string; pair: QAPair } | null>(null)
  const pair = opened?.key === scopeKey ? opened.pair : null
  const topic = useApi<TopicDetail>('voc2/topic', { ...filters, id: citation.id }, 0, citation.kind === 'topic')
  const knowledge = useApi<{ item: Knowledge }>('voc2/knowledge', { ...filters, id: citation.id }, 0, citation.kind === 'knowledge')
  const event = useApi<{ event: Evidence }>('voc2/event', { ...filters, id: citation.id }, 0, citation.kind === 'event')
  const query = citation.kind === 'topic' ? topic : citation.kind === 'knowledge' ? knowledge : event
  const evidence = citation.kind === 'topic' ? topic.data?.events || []
    : citation.kind === 'knowledge' ? knowledge.data?.item.evidence || []
      : event.data?.event ? [event.data.event] : []
  const cases = distinctCases(evidence)

  if (pair) return <div className="qiyu-page-enter space-y-3">
    <Button variant="ghost" size="sm" className="-ml-2" onClick={() => setOpened(null)}><ArrowLeft />返回示例案例</Button>
    <QADetail pair={pair} source="local" embedded onClose={() => setOpened(null)} />
  </div>

  return <div className="space-y-5">
    <div className="space-y-2">
      <h3 className="text-base font-semibold leading-6">{citation.title}</h3>
      <p className="text-xs leading-5 text-muted-foreground">{chatScopeLabel(filters)}</p>
    </div>
    <QueryPanel hasData={query.data !== null} loading={query.loading} error={query.error} retry={query.retry}>
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3"><h4 className="text-sm font-medium">示例案例</h4><span className="text-xs tabular-nums text-muted-foreground">{cases.length} 条</span></div>
        {citation.kind === 'knowledge' && cases.length > 0 && <p className="text-xs leading-5 text-muted-foreground">以下为知识引用的来源会话，包含历史案例，各案例按实际咨询时间展示。</p>}
        {cases.length ? cases.map((item, index) => {
          const id = qaIdentity(item)
          const question = item.question || item.question_quote || '未记录咨询原文'
          const reply = item.answer || item.claims.filter(claim => claim.role === 'staff').map(claim => claim.quote).join('\n')
          return <article key={id ?? `${item.id}-${index}`} className="qiyu-page-enter space-y-3 rounded-lg border bg-muted/15 p-4">
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-xs text-muted-foreground">
              <span>{customerLabel(item.customer_name, item.customer_id)}</span>
              <time>{consultationTime(item)}</time>
            </div>
            <p className="whitespace-pre-wrap break-words text-sm font-medium leading-6">{question}</p>
            <div className="space-y-1 text-xs leading-6 text-muted-foreground">
              <span>{item.staff_source_verified ? '客服回复摘要' : '来源回复摘要'}</span>
              <p className="line-clamp-3 whitespace-pre-wrap break-words">{reply || '当前问答片段未提取到回复，可在会话记录中查看前后文。'}</p>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs text-muted-foreground">{item.group_name || '未分组'}{item.staff_name ? ` · ${item.staff_name}` : ''}</span>
              {id !== null ? <Button variant="outline" size="sm" onClick={() => setOpened({ key: scopeKey, pair: normalizePair({ ...item, qa_id: id }) })}>查看详情</Button>
                : <span className="text-xs text-muted-foreground">会话记录暂不可定位</span>}
            </div>
          </article>
        }) : <p className="py-8 text-center text-sm text-muted-foreground">当前来源暂无可展示的会话案例。</p>}
      </div>
    </QueryPanel>
  </div>
}

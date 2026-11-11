import type { ReactElement } from 'react'
import { ArrowUpRight, Copy, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { useApi } from '@/hooks/use-api'
import type { ApiFilters } from '@/lib/api'
import { topicHref } from '@/lib/voc-topic-link'
import { LoadingState } from './loading-state'
import type { Knowledge, Topic } from './voc-next-workspace'
import type { TrendComparisonData } from './voc-topic-detail'

type TopicList = { items: Topic[]; total: number; comparison: TrendComparisonData }
type KnowledgeList = { items: Knowledge[] }
type BriefingRow = { id: string; title: string; description: string; href: string }
type BriefingSection = { title: string; total: number; rows: BriefingRow[] }
const previewLimit = 20
const formatCount = (value: number) => value.toLocaleString('zh-CN')

function validDay(day: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return false
  const date = new Date(`${day}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === day
}

function shanghaiDay(value: string): string {
  if (typeof value !== 'string') return ''
  const date = value.match(/^(\d{4}-\d{2}-\d{2})[T ]((?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)(\.\d+)?(Z|[+-]\d{2}:\d{2})?$/)
  if (!date || !validDay(date[1])) return ''
  const stamp = Date.parse(`${date[1]}T${date[2]}${date[3] || ''}${date[4] || '+08:00'}`)
  return Number.isFinite(stamp) ? new Date(stamp + 8 * 60 * 60 * 1000).toISOString().slice(0, 10) : ''
}

function issueLink(id: string, scope: ApiFilters): string {
  const hash = topicHref(id, scope)
  return hash.startsWith('#questions?') ? new URL(hash, window.location.href).toString() : ''
}

function summaryTitle(value: string): string {
  return value.replace(/https?:\/\/[^\s，。；！？]+/gi, '[链接]')
}

function topicRows(items: Topic[], trend: 'new' | 'rising', scope: ApiFilters): BriefingRow[] {
  return items.filter(item => item.trend?.status === trend).slice(0, previewLimit).map(item => {
    const delta = item.trend?.delta
    const count = `${formatCount(item.consultation_count ?? item.event_count)} 次咨询`
    return {
      id: item.id,
      title: summaryTitle(item.theme.trim() || item.question),
      description: trend === 'rising' && typeof delta === 'number' ? `${count}，较上期增加 ${formatCount(delta)} 次` : count,
      href: issueLink(item.id, scope),
    }
  })
}

function knowledgeRows(items: Knowledge[], scope: ApiFilters & { start: string; end: string }): BriefingSection {
  const eligible = items.flatMap(item => {
    const level = item.knowledge_reuse?.level
    const day = shanghaiDay(item.updated_at)
    if (item.status !== 'ready' || (level !== 'verified' && level !== 'guidance') || !day || day < scope.start || day > scope.end) return []
    return [{ item, day, level }]
  }).sort((a, b) => b.day.localeCompare(a.day) || a.item.id.localeCompare(b.item.id))
  return {
    title: '可复用知识更新', total: eligible.length,
    rows: eligible.slice(0, previewLimit).map(({ item, day, level }) => ({
      id: item.id,
      title: summaryTitle(item.question),
      description: `${level === 'verified' ? '效果已验证' : '已核验指引'} · ${day}`,
      href: issueLink(item.topic_id, scope),
    })),
  }
}

function markdownText(value: string) {
  return value.replace(/\s+/g, ' ').replace(/[\\`*_\[\]<>~]/g, '\\$&').trim()
}

function BriefingEntries({ section }: { section: BriefingSection }) {
  const { title, rows, total } = section
  return <section className="space-y-2.5">
    <div className="flex items-baseline justify-between gap-2"><h3 className="text-sm font-semibold">{title}</h3><span className="text-xs tabular-nums text-muted-foreground">{formatCount(total)} 条</span></div>
    {rows.length ? <ul className="divide-y rounded-lg border bg-background">
      {rows.map(row => <li key={row.id} className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1 px-3 py-2.5">
        <div className="min-w-0 flex-1 basis-48"><p className="break-words text-sm font-medium">{row.title}</p><p className="mt-0.5 text-xs tabular-nums text-muted-foreground">{row.description}</p></div>
        {row.href && <a href={row.href} target="_blank" rel="noreferrer" className="inline-flex shrink-0 items-center gap-1 rounded-sm text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-2">查看问题<ArrowUpRight aria-hidden="true" className="size-3" /></a>}
      </li>)}
    </ul> : <p className="rounded-lg border border-dashed bg-background px-3 py-2.5 text-sm text-muted-foreground">本期暂无{title}。</p>}
    {total > rows.length && <p className="text-xs text-muted-foreground">预览仅列出前 {previewLimit} 条。</p>}
  </section>
}

function BriefingContent({ filters }: { filters: ApiFilters }) {
  const scope = { source: filters.source, days: filters.days, start: filters.start, end: filters.end, group: filters.group, staff: filters.staff }
  const requestedStart = scope.start || ''
  const requestedEnd = scope.end || ''
  const requestedWindow = requestedStart || requestedEnd
    ? validDay(requestedStart) && validDay(requestedEnd) && requestedStart <= requestedEnd
    : Number.isInteger(scope.days) && (scope.days ?? 0) > 0
  const newTopics = useApi<TopicList>('voc2/topics', { ...scope, workbench: 1, trend: 'new', limit: previewLimit, offset: 0 }, 0, requestedWindow)
  const comparison = newTopics.data?.comparison
  const start = comparison?.current.start ?? ''
  const end = comparison?.current.end ?? ''
  const hasWindow = validDay(start) && validDay(end) && start <= end
  const pinnedScope = { ...scope, days: 0, start, end }
  const rising = useApi<TopicList>('voc2/topics', { ...pinnedScope, workbench: 1, trend: 'rising', limit: previewLimit, offset: 0 }, 0, hasWindow && comparison?.comparable === true)
  const knowledge = useApi<KnowledgeList>('voc2/knowledge', pinnedScope, 0, hasWindow)
  const failed = [newTopics, rising, knowledge].filter(query => query.error)
  const busy = [newTopics, rising, knowledge].some(query => query.loading || query.refreshing)
  const ready = hasWindow && !busy && failed.length === 0 && knowledge.data !== null
  const trendAvailable = ready && comparison?.comparable === true && rising.data?.comparison.comparable === true
  const trendNote = trendAvailable ? '' : rising.data?.comparison.note || comparison?.note || '本期覆盖不足，暂无法判断趋势。'
  const sections: BriefingSection[] = []
  if (trendAvailable && newTopics.data && rising.data) {
    sections.push(
      { title: '新出现的问题', total: newTopics.data.total, rows: topicRows(newTopics.data.items, 'new', pinnedScope) },
      { title: '明显上升的问题', total: rising.data.total, rows: topicRows(rising.data.items, 'rising', pinnedScope) },
    )
  }
  if (ready && knowledge.data) sections.push(knowledgeRows(knowledge.data.items, pinnedScope))
  const hasRows = sections.some(section => section.rows.length > 0)
  const period = start === end ? start : `${start} 至 ${end}`

  async function copy() {
    if (!ready || !hasRows) return
    const summary = ['# 语海Voc成果摘要', `统计区间 ${period}`]
    if (scope.group || scope.staff) summary.push('统计沿用预览时的筛选范围。')
    if (!trendAvailable) summary.push(`趋势暂不可判断。${trendNote}`)
    for (const section of sections) {
      if (!section.rows.length) continue
      summary.push('', `## ${section.title}（${formatCount(section.total)} 条）`)
      for (const row of section.rows) {
        const title = markdownText(row.title)
        const link = row.href ? `[查看问题](${row.href})` : ''
        summary.push(`- ${[title, row.description, link].filter(Boolean).join(' · ')}`)
      }
      if (section.total > section.rows.length) summary.push(`仅列出前 ${previewLimit} 条。`)
    }
    try {
      await navigator.clipboard.writeText(summary.join('\n'))
      toast.success('已复制摘要，可粘贴到飞书。')
    } catch {
      toast.error('无法复制，请手动复制所需内容。')
    }
  }

  return <>
    <div className="min-h-0 space-y-5 overflow-y-auto overscroll-contain px-0.5" aria-busy={busy}>
      {busy ? <LoadingState label="正在汇总成果…" /> : failed.length ? <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4 text-sm">
        <p>部分成果暂时无法读取，请重试。</p><Button size="sm" variant="outline" onClick={() => failed.forEach(query => query.retry())}><RefreshCw />重试</Button>
      </div> : !hasWindow ? <p className="rounded-lg border bg-muted/25 p-4 text-sm text-muted-foreground">选择最近一段时间或填写完整起止日期后，即可预览本期成果。</p> : <>
        <div className="rounded-xl border bg-linear-to-br from-background to-muted/50 p-3.5">
          <p className="text-sm font-medium tabular-nums">{period}</p>
          <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{[scope.group || '全部客户群', scope.staff || '全部客服'].join(' · ')}</p>
          {comparison && <p className="mt-1 text-xs tabular-nums text-muted-foreground">已整理 {formatCount(comparison.current.processed_pairs)} / {formatCount(comparison.current.source_pairs)} 条问答</p>}
        </div>
        {!trendAvailable && <p className="rounded-lg border bg-muted/25 p-3 text-sm leading-6 text-muted-foreground"><span className="font-medium text-foreground">趋势暂不可判断。</span>{trendNote}</p>}
        {sections.map(section => <BriefingEntries key={section.title} section={section} />)}
      </>}
    </div>
    <DialogFooter className="border-t pt-4">
      <DialogClose asChild><Button variant="outline">关闭</Button></DialogClose>
      <Button disabled={!ready || !hasRows} onClick={() => void copy()}><Copy />复制 Markdown 摘要</Button>
    </DialogFooter>
  </>
}

export function VocBriefingDialog({ open, onOpenChange, filters, trigger }: { open: boolean; onOpenChange: (open: boolean) => void; filters: ApiFilters; trigger?: ReactElement }) {
  return <Dialog open={open} onOpenChange={onOpenChange}>
    {trigger && <DialogTrigger asChild>{trigger}</DialogTrigger>}
    <DialogContent className="max-h-[min(86dvh,760px)] grid-rows-[auto_minmax(0,1fr)_auto] sm:max-w-2xl">
      <DialogHeader className="pr-7"><DialogTitle>飞书成果预览</DialogTitle><DialogDescription>查看当前统计区间的趋势和知识更新，复制摘要后可粘贴到飞书。</DialogDescription></DialogHeader>
      <BriefingContent filters={filters} />
    </DialogContent>
  </Dialog>
}

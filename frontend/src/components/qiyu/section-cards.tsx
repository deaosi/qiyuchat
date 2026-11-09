import { ArrowUpRight, Files, Layers3, MessageSquareText, UsersRound } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Card, CardAction, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import type { SummaryData } from '@/lib/api'

export function QiyuSectionCards({ summary, rangeLabel, loading = false, onNavigate }: {
  summary: SummaryData | null; rangeLabel: string; loading?: boolean; onNavigate: (page: string) => void
}) {
  const items = [
    { title: '有效问答', value: summary?.qa_pair, icon: Files, badge: rangeLabel, footer: `覆盖 ${summary?.group_count ?? '—'} 个客户群`, note: '当前筛选内抽取出的有效问答', page: 'pairs' },
    { title: '咨询客户', value: summary?.customer_count, icon: UsersRound, badge: '按 ID 去重', footer: '查看客户的咨询与历史问题', note: '匿名客户按服务端口径统计', page: 'customers' },
    { title: '问题簇', value: summary?.cluster_count, icon: Layers3, badge: '相似问法归并', footer: summary?.top[0]?.question || '按相似问法整理问题', note: '保留原话，便于逐条追溯与复核', page: 'clusters' },
    { title: '未发现回复', value: summary?.unanswered, icon: MessageSquareText, badge: summary ? `${summary.qa_pair ? (summary.unanswered / summary.qa_pair * 100).toFixed(1) : '0.0'}%` : '—', footer: '结合会话上下文进一步复核', note: '答案为空，不能直接判断是否已解决', page: 'unanswered' },
  ]
  return <div className="grid grid-cols-1 gap-4 px-4 *:data-[slot=card]:bg-gradient-to-t *:data-[slot=card]:from-primary/5 *:data-[slot=card]:to-card *:data-[slot=card]:shadow-xs lg:px-6 @xl/main:grid-cols-2 @5xl/main:grid-cols-4" aria-busy={loading}>
    {items.map(({ title, value, icon: Icon, badge, footer, note, page }) => <Card key={title} role="button" tabIndex={0} className="@container/card cursor-pointer transition-shadow hover:shadow-md focus-visible:outline-2 focus-visible:outline-ring" onClick={() => onNavigate(page)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onNavigate(page) } }}>
      <CardHeader><CardDescription>{title}</CardDescription><CardTitle className={`text-2xl font-semibold tabular-nums @[250px]/card:text-3xl ${loading ? 'animate-pulse' : ''}`}>{value === undefined ? '—' : value.toLocaleString('zh-CN')}</CardTitle><CardAction><Badge variant="outline"><Icon className="size-3.5" />{badge}</Badge></CardAction></CardHeader>
      <CardFooter className="flex-col items-start gap-1.5 text-sm"><div className="flex w-full items-center justify-between gap-2 font-medium"><span className="line-clamp-1" title={footer}>{footer}</span><ArrowUpRight className="size-4 shrink-0" /></div><div className="text-muted-foreground text-[13px]">{note}</div></CardFooter>
    </Card>)}
  </div>
}

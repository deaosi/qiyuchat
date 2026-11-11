import { useId } from 'react'
import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from 'recharts'
import { LoadingState } from './loading-state'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { TrendData } from '@/lib/api'

const config = { questions: { label: '有效问答', color: 'var(--primary)' } } satisfies ChartConfig
const dateFormatter = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', timeZone: 'Asia/Shanghai' })
const dateText = (value: string) => value ? dateFormatter.format(new Date(`${value}T00:00:00+08:00`)) : '—'

export function QiyuAreaChart({ data, days, onDaysChange, onDaySelect, animate, loading = false }: {
  data: TrendData | null; days: number; onDaysChange: (days: number) => void; onDaySelect: (day: string) => void; animate: boolean; loading?: boolean
}) {
  const id = useId().replace(/:/g, '')
  const points = data?.series ?? []
  const total = points.reduce((sum, point) => sum + point.questions, 0)
  const ceiling = Math.max(1, Math.ceil(Math.max(0, ...points.map(point => point.questions)) * 1.12))
  return <Card className="@container/card" aria-busy={loading}>
    <CardHeader><CardTitle>会话咨询趋势</CardTitle><CardDescription>{points.length ? `${dateText(points[0].date)} — ${dateText(points.at(-1)!.date)}，${total.toLocaleString('zh-CN')} 条有明确时间的有效问答` : '按服务器返回的自然日范围统计'}{Boolean(data?.unknown_time) && `；${data!.unknown_time} 条时间未知，未绘入趋势`}{data?.truncated && '；趋势最多展示最近 366 天'}</CardDescription><CardAction>
      <ToggleGroup type="single" value={String(days)} onValueChange={value => value && onDaysChange(Number(value))} variant="outline" className="hidden *:data-[slot=toggle-group-item]:px-4! @[767px]/card:flex"><ToggleGroupItem value="7">近 7 天</ToggleGroupItem><ToggleGroupItem value="30">近 30 天</ToggleGroupItem><ToggleGroupItem value="90">近 90 天</ToggleGroupItem><ToggleGroupItem value="0">全部</ToggleGroupItem></ToggleGroup>
      <Select value={String(days)} onValueChange={value => onDaysChange(Number(value))}><SelectTrigger size="sm" className="w-36 @[767px]/card:hidden" aria-label="图表时间范围"><SelectValue placeholder="自定义日期" /></SelectTrigger><SelectContent><SelectItem value="7">最近 7 天</SelectItem><SelectItem value="30">最近 30 天</SelectItem><SelectItem value="90">最近 90 天</SelectItem><SelectItem value="0">全部时间</SelectItem></SelectContent></Select>
    </CardAction></CardHeader>
    <CardContent className="px-2 pt-4 sm:px-6 sm:pt-6">{loading ? <LoadingState label="正在读取趋势…" rows={4} className="h-[260px] px-4 sm:px-0" /> : !points.length || total === 0 ? <div className="flex h-[260px] items-center justify-center px-6 text-center text-sm text-muted-foreground">当前范围没有可绘制的问答。可调整日期、客户群或客服筛选。</div> : <ChartContainer config={config} className={`aspect-auto h-[260px] w-full ${animate ? 'qiyu-chart-enter' : ''}`}><AreaChart data={points} margin={{ left: 5, right: 5 }} onClick={state => { if (state?.activeLabel) onDaySelect(String(state.activeLabel)) }}>
      <defs><linearGradient id={`${id}-questions`} x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="var(--color-questions)" stopOpacity={0.7} /><stop offset="95%" stopColor="var(--color-questions)" stopOpacity={0.04} /></linearGradient></defs>
      <CartesianGrid vertical={false} /><YAxis hide domain={[0, ceiling]} /><XAxis dataKey="date" tickLine={false} axisLine={false} tickMargin={9} minTickGap={32} tickFormatter={value => dateText(String(value))} />
      <ChartTooltip cursor={false} content={<ChartTooltipContent indicator="dot" labelFormatter={value => dateText(String(value))} />} />
      <Area dataKey="questions" type="monotone" fill={`url(#${id}-questions)`} stroke="var(--color-questions)" isAnimationActive={false} dot={points.length === 1} />
    </AreaChart></ChartContainer>}</CardContent>
  </Card>
}

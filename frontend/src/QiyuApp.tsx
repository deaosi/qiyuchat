import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react'
import { ArrowDownToLine, ArrowRight, CalendarDays, CircleHelp, CirclePlus, FileSpreadsheet, LayoutDashboard, Layers3, ListFilter, LoaderCircle, LogOut, MessageSquareText, Moon, Settings2, ShieldCheck, Sun, Upload, UsersRound, ContactRound, ChartNoAxesCombined, RefreshCw, SearchX, TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarHeader, SidebarInset, SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarProvider, SidebarTrigger, useSidebar } from '@/components/ui/sidebar'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Separator } from '@/components/ui/separator'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Toaster } from '@/components/ui/sonner'
import { QiyuSectionCards } from '@/components/qiyu/section-cards'
import { QiyuAreaChart } from '@/components/qiyu/area-chart'
import { QATable } from '@/components/qiyu/qa-table'
import { QADetail } from '@/components/qiyu/qa-detail'
import { ImportDialog } from '@/components/qiyu/import-dialog'
import { useApi } from '@/hooks/use-api'
import { apiPost, apiUrl, downloadExport, normalizePair, type ApiFilters, type ClusterItem, type LocalStatus, type MetaData, type SummaryData, type TrendData } from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'

const navigation = [
  { id: 'overview', title: '会话概览', icon: LayoutDashboard, group: '分析工作台' },
  { id: 'questions', title: '高频问题', icon: ChartNoAxesCombined, group: '问题洞察' },
  { id: 'clusters', title: '问题簇', icon: Layers3, group: '问题洞察' },
  { id: 'groups', title: '客户群', icon: UsersRound, group: '客户洞察' },
  { id: 'customers', title: '客户', icon: ContactRound, group: '客户洞察' },
  { id: 'pairs', title: '问答明细', icon: MessageSquareText, group: '会话资产' },
  { id: 'cleaning', title: '数据清洗', icon: ListFilter, group: '会话资产' },
] as const

type View = typeof navigation[number]['id']
type Reply = 'all' | 'unanswered' | 'answered'
interface GroupItem { group_name: string; total_questions: number; customer_count: number; unanswered: number; top: { cluster_id: number; question: string; count: number }[] }
interface CustomerItem { customer_id: string; customer_name: string; group_name: string; total_questions: number; distinct_clusters: number; unanswered: number; first_seen: string; last_seen: string }
type ListItem = ClusterItem | GroupItem | CustomerItem
const readView = (): View => navigation.some(item => item.id === location.hash.slice(1)) ? location.hash.slice(1) as View : 'overview'
const numberText = (value: unknown) => typeof value === 'number' ? value.toLocaleString('zh-CN') : '—'

function AppSidebar({ view, source, total, navigate, onImport }: { view: View; source: ApiFilters['source']; total?: number; navigate: (view: View) => void; onImport: () => void }) {
  const { setOpenMobile } = useSidebar()
  return <Sidebar collapsible="offcanvas" variant="inset">
    <SidebarHeader className="border-b px-4 py-4"><div className="flex h-7 items-center justify-between gap-2"><span className="text-base font-semibold tracking-tight">七鱼会话分析</span><Badge variant="outline" className="text-[10px] font-normal">{source === 'demo' ? '演示' : '工作台'}</Badge></div></SidebarHeader>
    <SidebarContent className="gap-1 pt-2">{[...new Set(navigation.map(item => item.group))].map(group => <SidebarGroup key={group}><SidebarGroupLabel>{group}</SidebarGroupLabel><SidebarGroupContent><SidebarMenu>{navigation.filter(item => item.group === group).map(item => <SidebarMenuItem key={item.id}><SidebarMenuButton isActive={view === item.id} onClick={() => { navigate(item.id); setOpenMobile(false) }} tooltip={item.title}><item.icon /><span>{item.title}</span>{item.id === 'pairs' && total !== undefined && <span className="ml-auto text-xs tabular-nums text-muted-foreground">{total.toLocaleString('zh-CN')}</span>}</SidebarMenuButton></SidebarMenuItem>)}</SidebarMenu></SidebarGroupContent></SidebarGroup>)}
      <SidebarGroup className="mt-auto"><SidebarGroupContent><SidebarMenu><SidebarMenuItem><SidebarMenuButton onClick={onImport}><CirclePlus /><span>导入会话数据</span></SidebarMenuButton></SidebarMenuItem><SidebarMenuItem><SidebarMenuButton onClick={() => toast.info('统计按有效问答计算；有回复不代表已解决。业务数据与独立演示库分别存放。')}><CircleHelp /><span>统计说明</span></SidebarMenuButton></SidebarMenuItem><SidebarMenuItem><SidebarMenuButton asChild><a href="/logout"><LogOut /><span>退出登录</span></a></SidebarMenuButton></SidebarMenuItem></SidebarMenu></SidebarGroupContent></SidebarGroup>
    </SidebarContent><SidebarFooter className="border-t p-3"><div className="flex items-center gap-3 rounded-lg px-2 py-1.5"><Avatar className="size-8 rounded-lg"><AvatarFallback className="rounded-lg text-xs">管理</AvatarFallback></Avatar><div className="grid flex-1 gap-0.5 text-sm"><span className="font-medium">管理员</span><span className="text-xs text-muted-foreground">{source === 'local' ? '业务数据工作台' : '独立演示数据'}</span></div><ShieldCheck className="size-4 text-muted-foreground" /></div></SidebarFooter>
  </Sidebar>
}

function QueryState({ loading, error, empty, retry, children }: { loading?: boolean; error?: string | null; empty?: boolean; retry?: () => void; children?: ReactNode }) {
  if (loading || error || empty) return <Card><CardContent className="flex min-h-44 flex-col items-center justify-center gap-3 py-10 text-center text-sm text-muted-foreground" role={error ? 'alert' : 'status'}>{loading ? <><LoaderCircle className="size-5 animate-spin" /><span>正在读取数据…</span></> : error ? <><TriangleAlert className="size-5" /><p className="max-w-lg">{error}</p><Button variant="outline" size="sm" onClick={retry}>重试</Button></> : <><SearchX className="size-5" /><p>当前筛选没有匹配的问答</p><p className="text-xs">可放宽日期、客户群或客服条件；时间未知的记录仅在全部时间中显示。</p></>}</CardContent></Card>
  return children
}

function Workspace() {
  const [view, setView] = useState<View>(readView)
  const [filters, setFilters] = useState<ApiFilters>({ source: 'local', days: 7 })
  const [drillFilters, setDrillFilters] = useState<Partial<ApiFilters>>({})
  const [exportFilters, setExportFilters] = useState<ApiFilters>(filters)
  const [initialReply, setInitialReply] = useState<Reply>('all')
  const [selected, setSelected] = useState<QAPair | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [cleanOpen, setCleanOpen] = useState(false)
  const [cleaning, setCleaning] = useState(false)
  const [preparingDemo, setPreparingDemo] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [range, setRange] = useState('7')
  const [customStart, setCustomStart] = useState('')
  const [customEnd, setCustomEnd] = useState('')
  const [motion, setMotion] = useState(true)
  const [compact, setCompact] = useState(false)
  const [theme, setTheme] = useState<'light' | 'dark'>(() => { try { return localStorage.getItem('qiyu-theme') === 'dark' ? 'dark' : 'light' } catch { return 'light' } })
  const source = filters.source
  const busy = cleaning || preparingDemo
  const effectiveFilters = useMemo<ApiFilters>(() => ({ ...filters, ...drillFilters }), [filters, drillFilters])
  const meta = useApi<MetaData>('meta', { source }, refreshKey)
  const summary = useApi<SummaryData>('summary', filters, refreshKey, view === 'overview')
  const trend = useApi<TrendData>('trend', filters, refreshKey, view === 'overview')
  const listEnabled = ['questions', 'clusters', 'groups', 'customers'].includes(view)
  const list = useApi<{ items: ListItem[]; total?: number }>(listEnabled ? view : 'questions', { ...filters, top: view === 'customers' ? 500 : view === 'groups' ? 3 : 100 }, refreshKey, listEnabled)
  const status = useApi<LocalStatus>('status', { source }, refreshKey, view === 'cleaning')
  const recent = useMemo(() => summary.data?.recent?.map(normalizePair) ?? [], [summary.data])
  const page = navigation.find(item => item.id === view)!
  const rangeLabel = filters.start || filters.end ? '自定义日期' : filters.days ? `近 ${filters.days} 天` : '全部时间'
  const scopeLabel = trend.data?.series.length ? `${trend.data.series[0].date} — ${trend.data.series.at(-1)!.date}` : rangeLabel

  useEffect(() => { const handler = () => setView(readView()); window.addEventListener('hashchange', handler); window.addEventListener('popstate', handler); return () => { window.removeEventListener('hashchange', handler); window.removeEventListener('popstate', handler) } }, [])
  useEffect(() => { document.documentElement.classList.toggle('dark', theme === 'dark'); try { localStorage.setItem('qiyu-theme', theme) } catch { /* Storage may be unavailable. */ } }, [theme])
  useEffect(() => setExportFilters(view === 'pairs' ? effectiveFilters : filters), [view, effectiveFilters, filters])
  const navigate = (next: View) => { setView(next); if (location.hash !== `#${next}`) history.pushState(null, '', `#${next}`) }
  const sideNavigate = (next: View) => { setDrillFilters({}); setInitialReply('all'); navigate(next) }
  const drill = (next: Partial<ApiFilters> = {}, reply: Reply = 'all') => { setDrillFilters(next); setInitialReply(reply); navigate('pairs') }
  const changeSource = (next: ApiFilters['source']) => { setSelected(null); setFilters({ source: next, days: 7 }); setDrillFilters({}); setInitialReply('all'); setRange('7') }
  const openImport = () => { if (busy) return; if (source !== 'local') changeSource('local'); setImportOpen(true) }
  const changeDays = (days: number) => { setRange(String(days)); setFilters(current => ({ ...current, days, start: undefined, end: undefined })); setDrillFilters({}) }
  const reset = () => { setFilters({ source, days: 7 }); setRange('7'); setCustomStart(''); setCustomEnd(''); setDrillFilters({}); setInitialReply('all') }
  const applyCustom = () => { if (!customStart && !customEnd) { toast.error('请至少填写一个日期边界'); return } if (customStart && customEnd && customStart > customEnd) { toast.error('开始日期不能晚于结束日期'); return } setFilters(current => ({ ...current, days: 0, start: customStart, end: customEnd })); setDrillFilters({}) }
  const cardNavigate = (next: string) => next === 'unanswered' ? drill({}, 'unanswered') : next === 'pairs' ? drill() : sideNavigate(next as View)
  const clean = async () => { setCleanOpen(false); setCleaning(true); try { const result = await apiPost<{ sessions_scanned: number; qa_pairs: number; clusters: number }>('clean', { source }); setDrillFilters({}); setSelected(null); setRefreshKey(key => key + 1); toast.success(`规则清洗完成：处理 ${result.sessions_scanned} 个会话，${result.qa_pairs} 条问答，${result.clusters} 个问题簇。`) } catch (error) { toast.error(error instanceof Error ? error.message : '清洗失败，请重试。') } finally { setCleaning(false) } }
  const prepareDemo = async () => { setPreparingDemo(true); try { await apiPost('demo', { source: 'demo' }); changeSource('demo'); setRange('0'); setFilters({ source: 'demo', days: 0 }); setRefreshKey(key => key + 1); navigate('overview'); toast.success('独立演示库已准备完成') } catch (error) { toast.error(error instanceof Error ? error.message : '无法准备演示数据') } finally { setPreparingDemo(false) } }
  const exportCsv = async () => { setExporting(true); try { await downloadExport(view === 'pairs' || view === 'overview' ? exportFilters : filters); toast.success('已下载当前范围的脱敏问答') } catch (error) { toast.error(error instanceof Error ? error.message : '导出失败，请重试。') } finally { setExporting(false) } }

  return <SidebarProvider style={{ '--sidebar-width': 'calc(var(--spacing) * 64)', '--header-height': 'calc(var(--spacing) * 14)' } as CSSProperties}>
    <AppSidebar view={view} source={source} total={meta.data?.total} navigate={sideNavigate} onImport={openImport} />
    <SidebarInset data-density={compact ? 'compact' : 'normal'} data-motion={motion ? 'on' : 'off'}>
      <header className="flex h-(--header-height) shrink-0 items-center gap-2 border-b transition-[width,height] ease-linear"><div className="flex w-full items-center gap-1 px-4 lg:gap-2 lg:px-6"><SidebarTrigger className="-ml-1" /><Separator orientation="vertical" className="mx-2 data-[orientation=vertical]:h-4" /><h1 className="text-base font-medium">{page.title}</h1><div className="ml-auto flex items-center gap-2">
        <Select value={source} onValueChange={value => changeSource(value as ApiFilters['source'])} disabled={busy}><SelectTrigger size="sm" className="w-32" aria-label="数据源"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="local">业务数据</SelectItem><SelectItem value="demo">独立演示库</SelectItem></SelectContent></Select>
        <DropdownMenu><DropdownMenuTrigger asChild><Button variant="ghost" size="icon" aria-label="显示设置"><Settings2 className="size-4" /></Button></DropdownMenuTrigger><DropdownMenuContent align="end" className="w-48"><DropdownMenuLabel>显示设置</DropdownMenuLabel><DropdownMenuSeparator /><DropdownMenuCheckboxItem checked={motion} onCheckedChange={setMotion}>切换动画</DropdownMenuCheckboxItem><DropdownMenuCheckboxItem checked={compact} onCheckedChange={setCompact}>紧凑表格</DropdownMenuCheckboxItem><DropdownMenuSeparator /><DropdownMenuItem onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun /> : <Moon />}切换{theme === 'dark' ? '浅色' : '深色'}外观</DropdownMenuItem></DropdownMenuContent></DropdownMenu>
        <Button size="sm" onClick={openImport} disabled={busy}><CirclePlus /><span className="hidden sm:inline">导入会话</span></Button>
      </div></div></header>
      <div className="flex flex-1 flex-col"><div className="@container/main flex flex-1 flex-col gap-2"><div key={view} className={`flex flex-col gap-4 py-4 md:gap-6 md:py-6 ${motion ? 'qiyu-page-enter' : ''}`}>
        {source === 'demo' && <div className="mx-4 flex flex-wrap items-center gap-3 rounded-lg border bg-muted/40 px-4 py-3 text-sm lg:mx-6"><Badge variant="outline">演示库</Badge><span className="flex-1 text-muted-foreground">模拟数据独立存放，不计入业务统计。</span>{meta.data?.total === 0 && <Button size="sm" variant="outline" onClick={prepareDemo} disabled={busy}>{preparingDemo && <LoaderCircle className="animate-spin" />}载入演示数据</Button>}</div>}
        {meta.error && <div className="px-4 lg:px-6"><QueryState error={meta.error} retry={meta.retry} /></div>}
        {view !== 'cleaning' && <div className="flex flex-wrap items-center gap-2 px-4 lg:px-6">
          <div className="mr-auto hidden items-center gap-2 text-sm text-muted-foreground xl:flex"><CalendarDays className="size-4" />{scopeLabel}</div>
          <Select value={filters.group || 'all'} onValueChange={value => { setFilters(current => ({ ...current, group: value === 'all' ? '' : value })); setDrillFilters({}) }}><SelectTrigger size="sm" className="w-[156px]" aria-label="客户群筛选"><UsersRound className="size-3.5" /><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部客户群</SelectItem>{meta.data?.groups.map(group => <SelectItem key={group} value={group}>{group}</SelectItem>)}</SelectContent></Select>
          <Select value={filters.staff || 'all'} onValueChange={value => { setFilters(current => ({ ...current, staff: value === 'all' ? '' : value })); setDrillFilters({}) }}><SelectTrigger size="sm" className="w-[134px]" aria-label="客服筛选"><ContactRound className="size-3.5" /><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部客服</SelectItem>{meta.data?.staff.map(staff => <SelectItem key={staff} value={staff}>{staff}</SelectItem>)}</SelectContent></Select>
          <Select value={range} onValueChange={value => { setRange(value); if (value !== 'custom') changeDays(Number(value)) }}><SelectTrigger size="sm" className="w-[128px]" aria-label="时间筛选"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="7">最近 7 天</SelectItem><SelectItem value="30">最近 30 天</SelectItem><SelectItem value="90">最近 90 天</SelectItem><SelectItem value="0">全部时间</SelectItem><SelectItem value="custom">自定义日期</SelectItem></SelectContent></Select>
          <Button variant="ghost" size="sm" onClick={reset}>重置</Button><Button variant="ghost" size="icon-sm" onClick={() => setRefreshKey(key => key + 1)} aria-label="刷新数据"><RefreshCw className="size-3.5" /></Button><Button variant="outline" size="sm" onClick={exportCsv} disabled={exporting}>{exporting ? <LoaderCircle className="size-3.5 animate-spin" /> : <ArrowDownToLine className="size-3.5" />}<span className="hidden sm:inline">导出脱敏问答</span></Button>
        </div>}
        {range === 'custom' && view !== 'cleaning' && <form className="flex flex-wrap items-center gap-3 px-4 text-sm lg:px-6" onSubmit={event => { event.preventDefault(); applyCustom() }}><label className="flex items-center gap-2">开始日期<Input type="date" value={customStart} onChange={event => setCustomStart(event.target.value)} min="2000-01-01" max="2100-12-31" className="w-40" /></label><label className="flex items-center gap-2">结束日期<Input type="date" value={customEnd} onChange={event => setCustomEnd(event.target.value)} min="2000-01-01" max="2100-12-31" className="w-40" /></label><Button size="sm" type="submit">应用日期</Button></form>}
        {view === 'overview' && <>
          <QiyuSectionCards summary={summary.data} rangeLabel={rangeLabel} loading={summary.loading} onNavigate={cardNavigate} />
          {summary.error && <div className="px-4 lg:px-6"><QueryState error={summary.error} retry={summary.retry} /></div>}
          <div className="px-4 lg:px-6">{trend.error ? <QueryState error={trend.error} retry={trend.retry} /> : <QiyuAreaChart data={trend.data} loading={trend.loading} days={filters.start || filters.end ? -1 : filters.days ?? 0} onDaysChange={changeDays} onDaySelect={day => drill({ days: 0, start: day, end: day })} animate={motion} />}</div>
          <div className="px-4 lg:px-6"><QATable key={`overview-${source}`} data={recent} remote={{ filters, refreshKey }} onSelect={setSelected} onFiltersChange={setExportFilters} /></div>
        </>}
        {view === 'pairs' && <div className="space-y-4 px-4 lg:px-6">{Object.values(drillFilters).some(value => value !== undefined && value !== '') && <div className="flex flex-wrap items-center gap-2"><span className="text-sm text-muted-foreground">限定条件</span>{drillFilters.cluster && <Badge variant="secondary">问题簇 {drillFilters.cluster}</Badge>}{drillFilters.customer && <Badge variant="secondary">客户 {drillFilters.customer}</Badge>}{drillFilters.group && <Badge variant="secondary">{drillFilters.group}</Badge>}{drillFilters.start && <Badge variant="secondary">{drillFilters.start}{drillFilters.end !== drillFilters.start ? ` 至 ${drillFilters.end || '不限'}` : ''}</Badge>}<Button variant="ghost" size="xs" onClick={() => { setDrillFilters({}); setInitialReply('all') }}>清除限定条件</Button></div>}<QATable key={`${source}-${JSON.stringify(drillFilters)}-${initialReply}`} data={[]} remote={{ filters: effectiveFilters, refreshKey }} onSelect={setSelected} initialReply={initialReply} onFiltersChange={setExportFilters} /></div>}
        {listEnabled && <div className="px-4 lg:px-6"><QueryState loading={list.loading} error={list.error} retry={list.retry} empty={!list.data?.items.length}>
          {view === 'questions' && <Card><CardHeader><CardTitle>高频问题</CardTitle><CardDescription>按当前筛选中的问答次数展示前 {list.data?.items.length ?? 0} 个问题簇，点击追溯原话。</CardDescription></CardHeader><CardContent><div className="overflow-hidden rounded-lg border"><Table><TableHeader><TableRow><TableHead className="w-16">排名</TableHead><TableHead>代表问题</TableHead><TableHead className="text-right">问答数</TableHead><TableHead className="text-right">客户数</TableHead><TableHead className="text-right">占比</TableHead><TableHead /></TableRow></TableHeader><TableBody>{(list.data?.items as ClusterItem[] | undefined)?.map((item, index) => <TableRow key={item.cluster_id}><TableCell className="tabular-nums text-muted-foreground">{String(index + 1).padStart(2, '0')}</TableCell><TableCell className="max-w-xl"><button className="text-left text-sm font-medium hover:underline" onClick={() => drill({ cluster: item.cluster_id })}>{item.question}</button><div className="mt-1 line-clamp-1 text-xs text-muted-foreground">{item.variants} 种问法 · 簇 {item.cluster_id}</div></TableCell><TableCell className="text-right tabular-nums">{numberText(item.count)}</TableCell><TableCell className="text-right tabular-nums">{numberText(item.customers)}</TableCell><TableCell className="text-right tabular-nums">{item.share.toFixed(1)}%</TableCell><TableCell><Button variant="ghost" size="icon-sm" onClick={() => drill({ cluster: item.cluster_id })} aria-label="追溯该问题簇"><ArrowRight /></Button></TableCell></TableRow>)}</TableBody></Table></div></CardContent></Card>}
          {view === 'clusters' && <div className="grid gap-4 lg:grid-cols-2">{(list.data?.items as ClusterItem[] | undefined)?.map(item => <Card key={item.cluster_id}><CardHeader><div className="mb-1 flex items-center justify-between"><Badge variant="outline">问题簇 {item.cluster_id}</Badge><span className="text-xs text-muted-foreground">{item.variants} 种问法</span></div><CardTitle className="text-lg leading-relaxed">{item.question}</CardTitle><CardDescription>{item.count} 条问答 · {item.customers} 位客户 · {item.groups} 个客户群</CardDescription></CardHeader><CardContent><ul className="space-y-2 text-sm text-muted-foreground">{item.samples.map(sample => <li key={sample} className="flex gap-2"><MessageSquareText className="mt-0.5 size-4 shrink-0 opacity-50" /><span>{sample}</span></li>)}</ul></CardContent><CardFooter className="justify-between gap-2"><Badge variant="secondary">{item.unanswered} 条未发现回复</Badge><Button variant="outline" size="sm" onClick={() => drill({ cluster: item.cluster_id })}>追溯问答<ArrowRight /></Button></CardFooter></Card>)}</div>}
          {view === 'groups' && <Card><CardHeader><CardTitle>客户群咨询分布</CardTitle><CardDescription>按当前范围的有效问答量排序，点击客户群或高频问题下钻。</CardDescription></CardHeader><CardContent><div className="overflow-x-auto"><Table><TableHeader><TableRow><TableHead>客户群</TableHead><TableHead>有效问答</TableHead><TableHead>客户数</TableHead><TableHead>未发现回复</TableHead><TableHead>高频问题</TableHead></TableRow></TableHeader><TableBody>{(list.data?.items as GroupItem[] | undefined)?.map(item => <TableRow key={item.group_name}><TableCell><Button variant="link" className="p-0 text-foreground" onClick={() => drill({ group: item.group_name })}>{item.group_name}</Button></TableCell><TableCell>{numberText(item.total_questions)}</TableCell><TableCell>{numberText(item.customer_count)}</TableCell><TableCell>{numberText(item.unanswered)}</TableCell><TableCell className="max-w-md">{item.top.slice(0, 3).map(question => <button key={question.cluster_id} className="block text-left text-sm hover:underline" onClick={() => drill({ group: item.group_name, cluster: question.cluster_id })}>{question.question} <span className="text-muted-foreground">{question.count} 次</span></button>)}</TableCell></TableRow>)}</TableBody></Table></div></CardContent></Card>}
          {view === 'customers' && <Card><CardHeader><CardTitle>客户咨询记录</CardTitle><CardDescription>按问答量展示前 {list.data?.items.length ?? 0} 位客户，最多 500 位；客户 ID 一致时合并。</CardDescription></CardHeader><CardContent><div className="overflow-x-auto"><Table><TableHeader><TableRow><TableHead>客户</TableHead><TableHead>客户群</TableHead><TableHead>问答数</TableHead><TableHead>问题簇</TableHead><TableHead>未发现回复</TableHead><TableHead>最近咨询</TableHead></TableRow></TableHeader><TableBody>{(list.data?.items as CustomerItem[] | undefined)?.map(item => <TableRow key={item.customer_id}><TableCell><Button variant="link" className="h-auto p-0 text-foreground" onClick={() => drill({ customer: item.customer_id })}>{item.customer_name || '匿名客户'}</Button><div className="mt-1 text-xs text-muted-foreground">{item.customer_id}</div></TableCell><TableCell>{item.group_name}</TableCell><TableCell>{numberText(item.total_questions)}</TableCell><TableCell>{numberText(item.distinct_clusters)}</TableCell><TableCell>{numberText(item.unanswered)}</TableCell><TableCell className="text-muted-foreground">{item.last_seen || '时间未知'}</TableCell></TableRow>)}</TableBody></Table></div></CardContent></Card>}
        </QueryState></div>}
        {view === 'cleaning' && <div className="space-y-6 px-4 lg:px-6"><div className="grid gap-4 lg:grid-cols-2"><Card><CardHeader><FileSpreadsheet className="mb-3 size-6" /><CardTitle>导入会话数据</CardTitle><CardDescription>上传 CSV / XLSX，先检查字段和脱敏样本，确认后写入业务库并执行规则清洗。</CardDescription></CardHeader><CardFooter className="gap-3"><Button onClick={openImport} disabled={busy}><Upload />选择文件并预览</Button><Button variant="outline" asChild><a href={apiUrl('template', { source: 'local' })}>下载模板</a></Button></CardFooter></Card><Card><CardHeader><ListFilter className="mb-3 size-6" /><CardTitle>重新执行规则清洗</CardTitle><CardDescription>重新抽取当前{source === 'local' ? '业务' : '演示'}库保留的会话，配对问答并重算问题簇。不会调用模型。</CardDescription></CardHeader><CardFooter><Button variant="outline" onClick={() => setCleanOpen(true)} disabled={busy || !status.data?.sessions}>{cleaning ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}{cleaning ? '正在规则清洗' : '重新规则清洗'}</Button></CardFooter></Card></div>
          <QueryState loading={status.loading} error={status.error} retry={status.retry}><div className="grid gap-4 lg:grid-cols-[1fr_1.6fr]"><Card><CardHeader><CardTitle>处理状态</CardTitle><CardDescription>当前数据源全库统计，不受分析页筛选影响。</CardDescription></CardHeader><CardContent><dl className="space-y-3 text-sm">{[['原始会话', 'sessions'], ['有正文的会话', 'with_content'], ['已处理', 'processed'], ['待规则清洗', 'pending'], ['抽取到问答', 'with_pairs'], ['正文无有效问题', 'no_question'], ['缺少正文', 'no_content'], ['正文已归档', 'archived'], ['有效问答', 'total_pairs']].map(([label, key]) => <div key={key} className="flex justify-between gap-3"><dt className="text-muted-foreground">{label}</dt><dd className="tabular-nums">{numberText(status.data?.[key])}</dd></div>)}</dl></CardContent><CardFooter className="text-xs text-muted-foreground">规则版本 {status.data?.version || '—'}</CardFooter></Card><Card><CardHeader><CardTitle>最近处理记录</CardTitle><CardDescription>当前数据源的导入与规则清洗结果。</CardDescription></CardHeader><CardContent>{status.data?.tasks.length ? <div className="overflow-x-auto"><Table><TableHeader><TableRow><TableHead>时间</TableHead><TableHead>操作</TableHead><TableHead>摘要</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>{status.data.tasks.map((task, index) => <TableRow key={`${task.created_at_text}-${index}`}><TableCell className="whitespace-nowrap">{task.created_at_text}</TableCell><TableCell className="whitespace-nowrap">{task.kind === 'local_import' ? '导入并清洗' : '规则清洗'}</TableCell><TableCell className="min-w-48 text-sm text-muted-foreground">{task.kind === 'local_import' ? `${String(task.result.filename || '文件')}：新增 ${task.result.new_sessions ?? 0} / 更新 ${task.result.updated_sessions ?? 0} 个会话；跳过错误行 ${task.result.skipped_rows ?? 0}` : `处理 ${task.result.sessions_scanned ?? 0} 个会话，${task.result.qa_pairs ?? 0} 条问答`}</TableCell><TableCell><Badge variant="outline">{task.status === 'success' || task.status === 'ok' ? '完成' : task.status || '已记录'}</Badge></TableCell></TableRow>)}</TableBody></Table></div> : <p className="py-8 text-center text-sm text-muted-foreground">暂无本地处理记录</p>}</CardContent></Card></div></QueryState>
          <Card><CardHeader><CardTitle>独立演示数据</CardTitle><CardDescription>准备或重置演示库，用于熟悉界面操作。业务库数据保持独立。</CardDescription></CardHeader><CardFooter><Button variant="outline" onClick={prepareDemo} disabled={busy}>{preparingDemo && <LoaderCircle className="animate-spin" />}载入演示库</Button></CardFooter></Card>
        </div>}
        <footer className="flex items-center justify-between gap-4 px-4 pt-1 text-xs text-muted-foreground lg:px-6"><span className="flex items-center gap-1.5"><ShieldCheck className="size-3.5" />{source === 'demo' ? '当前为独立模拟演示数据' : '问答与会话内容按服务端规则脱敏'}</span><span>{meta.data?.db_name || '七鱼会话分析'}</span></footer>
      </div></div></div>
    </SidebarInset>
    <QADetail pair={selected} source={source} onClose={() => setSelected(null)} />
    <ImportDialog open={importOpen} onOpenChange={setImportOpen} onImported={() => { setFilters({ source: 'local', days: 0 }); setRange('0'); setDrillFilters({}); setSelected(null); setRefreshKey(key => key + 1); navigate('cleaning') }} />
    <AlertDialog open={cleanOpen} onOpenChange={setCleanOpen}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>重新执行{source === 'local' ? '业务库' : '演示库'}规则清洗？</AlertDialogTitle><AlertDialogDescription>将重新抽取当前库保留的会话并重算问题簇，簇编号可能变化。原始正文不会删除，没有正文的归档问答会保留。不调用模型或外部服务。</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction onClick={() => void clean()}>确认重新清洗</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
    <Toaster position="top-center" theme={theme} />
  </SidebarProvider>
}

export default function QiyuApp() { return <Workspace /> }

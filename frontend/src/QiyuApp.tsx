import { memo, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { ArrowDownToLine, ArrowLeft, BookOpen, CalendarDays, CircleHelp, CirclePlus, FileSpreadsheet, FileText, History, LayoutDashboard, Layers3, ListFilter, LoaderCircle, LogOut, MessagesSquare, MessageSquareText, Moon, Settings2, ShieldCheck, Sun, Upload, UsersRound, ContactRound, ChartNoAxesCombined, RefreshCw, SearchX, SlidersHorizontal, TriangleAlert } from 'lucide-react'
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
import { AudienceWorkspace } from '@/components/qiyu/audience-workspace'
import { RuleClusterWorkspace } from '@/components/qiyu/rule-cluster-workspace'
import { VocFrequencyWorkspace } from '@/components/qiyu/voc-next-workspace'
import { VocKnowledgeLibrary } from '@/components/qiyu/voc-sop-workspace'
import { VocChatWorkspace } from '@/components/qiyu/voc-chat-workspace'
import { VocBriefingDialog } from '@/components/qiyu/voc-briefing-dialog'
import { VocJobHistory } from '@/components/qiyu/voc-job-history'
import { chatHref, normalizeChatFilters, readChatFilters } from '@/lib/voc-chat'
import { readTopicLink } from '@/lib/voc-topic-link'
import { ImportDialog } from '@/components/qiyu/import-dialog'
import { GatewaySettings } from '@/components/qiyu/gateway-settings'
import { LoadingState, PageActivity, RefreshDataButton } from '@/components/qiyu/loading-state'
import { useApi } from '@/hooks/use-api'
import { apiPost, downloadExport, downloadTemplate, normalizePair, type ApiFilters, type LocalStatus, type MetaData, type SummaryData, type TrendData } from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'
import { customerLabel } from '@/lib/utils'

const navigation = [
  { id: 'overview', title: '会话概览', icon: LayoutDashboard, group: '分析工作台' },
  { id: 'questions', title: 'VOC高频问题', icon: ChartNoAxesCombined, group: '问题洞察' },
  { id: 'voc-next', title: '沉淀知识库', icon: BookOpen, group: '问题洞察' },
  { id: 'voc-chat', title: '问问数据', icon: MessagesSquare, group: '问题洞察' },
  { id: 'clusters', title: '规则问题簇', icon: Layers3, group: '会话资产' },
  { id: 'groups', title: '客户群', icon: UsersRound, group: '客户洞察' },
  { id: 'customers', title: '客户', icon: ContactRound, group: '客户洞察' },
  { id: 'pairs', title: '问答明细', icon: MessageSquareText, group: '会话资产' },
  { id: 'cleaning', title: '数据清洗', icon: ListFilter, group: '会话资产' },
  { id: 'settings', title: '模型网关', icon: SlidersHorizontal, group: '系统设置' },
] as const

const sidebarPages = navigation.filter(({ id }) => id !== 'settings' && id !== 'clusters')
const sidebarGroups = [...new Set(sidebarPages.map(({ group }) => group))]

type View = typeof navigation[number]['id']
type Reply = 'all' | 'unanswered' | 'answered'
const readView = (): View => { const id = location.hash.slice(1).split('?')[0]; return navigation.some(item => item.id === id) ? id as View : 'overview' }
const readCustomerGroup = (): string => readView() === 'customers' && typeof history.state?.customerGroup === 'string' ? history.state.customerGroup : ''
const numberText = (value: unknown) => typeof value === 'number' ? value.toLocaleString('zh-CN') : '—'

const AppSidebar = memo(function AppSidebar({ view, total, navigate, onImport }: { view: View; total?: number; navigate: (view: View) => void; onImport: () => void }) {
  const { setOpenMobile } = useSidebar()
  const activePage = view === 'clusters' ? 'cleaning' : view
  return <Sidebar collapsible="offcanvas" variant="inset">
    <SidebarHeader className="border-b px-4 py-4"><div className="flex h-7 items-center justify-between gap-2"><span className="text-base font-semibold tracking-tight">语海Voc</span><Badge variant="outline" className="text-[10px] font-normal">工作台</Badge></div></SidebarHeader>
    <SidebarContent className="gap-1 pt-2">{sidebarGroups.map(group => <SidebarGroup key={group}><SidebarGroupLabel>{group}</SidebarGroupLabel><SidebarGroupContent><SidebarMenu>{sidebarPages.filter(item => item.group === group).map(item => <SidebarMenuItem key={item.id}><SidebarMenuButton isActive={activePage === item.id} onClick={() => { navigate(item.id); setOpenMobile(false) }} tooltip={item.title}><item.icon /><span>{item.title}</span>{item.id === 'pairs' && total !== undefined && <span className="ml-auto text-xs tabular-nums text-muted-foreground">{total.toLocaleString('zh-CN')}</span>}</SidebarMenuButton></SidebarMenuItem>)}</SidebarMenu></SidebarGroupContent></SidebarGroup>)}
      <SidebarGroup className="mt-auto"><SidebarGroupLabel>系统设置</SidebarGroupLabel><SidebarGroupContent><SidebarMenu><SidebarMenuItem><SidebarMenuButton isActive={view === 'settings'} onClick={() => { navigate('settings'); setOpenMobile(false) }} tooltip="模型网关"><SlidersHorizontal /><span>模型网关</span></SidebarMenuButton></SidebarMenuItem><SidebarMenuItem><SidebarMenuButton onClick={onImport}><CirclePlus /><span>导入会话数据</span></SidebarMenuButton></SidebarMenuItem><SidebarMenuItem><SidebarMenuButton onClick={() => toast.info('统计按业务会话中的有效问答计算；有回复不代表已解决。')}><CircleHelp /><span>统计说明</span></SidebarMenuButton></SidebarMenuItem><SidebarMenuItem><SidebarMenuButton asChild><a href="/logout"><LogOut /><span>退出登录</span></a></SidebarMenuButton></SidebarMenuItem></SidebarMenu></SidebarGroupContent></SidebarGroup>
    </SidebarContent><SidebarFooter className="border-t p-3"><div className="flex items-center gap-3 rounded-lg px-2 py-1.5"><Avatar className="size-8 rounded-lg"><AvatarFallback className="rounded-lg text-xs">管理</AvatarFallback></Avatar><div className="grid flex-1 gap-0.5 text-sm"><span className="font-medium">管理员</span><span className="text-xs text-muted-foreground">业务数据工作台</span></div><ShieldCheck className="size-4 text-muted-foreground" /></div></SidebarFooter>
  </Sidebar>
})

function QueryState({ loading, error, empty, retry, children }: { loading?: boolean; error?: string | null; empty?: boolean; retry?: () => void; children?: ReactNode }) {
  if (loading) return <Card><CardContent><LoadingState /></CardContent></Card>
  if (error || empty) return <Card><CardContent className="flex min-h-44 flex-col items-center justify-center gap-3 py-10 text-center text-sm text-muted-foreground" role={error ? 'alert' : 'status'}>{error ? <><TriangleAlert className="size-5" /><p className="max-w-lg">{error}</p><Button variant="outline" size="sm" onClick={retry}>重试</Button></> : <><SearchX className="size-5" /><p>当前筛选没有匹配的问答</p><p className="text-xs">可放宽日期、客户群或客服条件；时间未知的记录仅在全部时间中显示。</p></>}</CardContent></Card>
  return children
}

function Workspace() {
  const [view, setView] = useState<View>(readView)
  const [filters, setFilters] = useState<ApiFilters>(() => readChatFilters(location.hash) || readTopicLink(location.hash)?.filters || { source: 'local', days: 7 })
  const [linkedTopic, setLinkedTopic] = useState(() => readTopicLink(location.hash)?.id ?? null)
  const [briefingOpen, setBriefingOpen] = useState(false)
  const [jobsOpen, setJobsOpen] = useState(false)
  const [drillFilters, setDrillFilters] = useState<Partial<ApiFilters>>({})
  const [customerGroup, setCustomerGroup] = useState(readCustomerGroup)
  const [drillCustomerName, setDrillCustomerName] = useState('')
  const [tableExport, setTableExport] = useState<{ key: string; filters: ApiFilters } | null>(null)
  const [initialReply, setInitialReply] = useState<Reply>('all')
  const [selected, setSelected] = useState<{ pair: QAPair; anchorMessageId?: string; sessionContext: boolean } | null>(null)
  const pairOpener = useRef<HTMLElement | null>(null)
  const openPair = useCallback((pair: QAPair, anchorMessageId?: string, sessionContext = false) => {
    pairOpener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setSelected({ pair, anchorMessageId, sessionContext })
  }, [])
  const restorePairFocus = useCallback((event: Event) => {
    const opener = pairOpener.current
    pairOpener.current = null
    if (opener?.isConnected && opener !== document.body) {
      event.preventDefault()
      opener.focus({ preventScroll: true })
    }
  }, [])
  const [importOpen, setImportOpen] = useState(false)
  const [cleanOpen, setCleanOpen] = useState(false)
  const [cleaning, setCleaning] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [downloadingTemplate, setDownloadingTemplate] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [range, setRange] = useState(() => filters.start || filters.end ? 'custom' : String(filters.days ?? 7))
  const [customStart, setCustomStart] = useState(filters.start || '')
  const [customEnd, setCustomEnd] = useState(filters.end || '')
  const [motion, setMotion] = useState(true)
  const [compact, setCompact] = useState(false)
  const [theme, setTheme] = useState<'light' | 'dark'>(() => { try { return localStorage.getItem('qiyu-theme') === 'dark' ? 'dark' : 'light' } catch { return 'light' } })
  const source = 'local' as const
  const busy = cleaning
  const { days, start, end, group, staff } = filters
  const sharedScope = useMemo<ApiFilters>(() => ({ source, days, start, end, group, staff }), [days, start, end, group, staff])
  const effectiveFilters = useMemo<ApiFilters>(() => ({ ...sharedScope, ...drillFilters }), [sharedScope, drillFilters])
  const audienceScope = useMemo<ApiFilters>(() => customerGroup ? { ...sharedScope, group: customerGroup } : sharedScope, [sharedScope, customerGroup])
  const activeGroup = view === 'customers' ? audienceScope.group : group
  const tableScope = view === 'pairs' ? effectiveFilters : view === 'customers' ? audienceScope : sharedScope
  const tableExportKey = JSON.stringify([view, tableScope, view === 'pairs' ? initialReply : 'all'])
  const captureTableFilters = useCallback((filters: ApiFilters) => setTableExport({ key: tableExportKey, filters }), [tableExportKey])
  const meta = useApi<MetaData>('meta', { source }, refreshKey)
  const summary = useApi<SummaryData>('summary', sharedScope, refreshKey, view === 'overview')
  const trend = useApi<TrendData>('trend', sharedScope, refreshKey, view === 'overview')
  const status = useApi<LocalStatus>('status', { source }, refreshKey, view === 'cleaning')
  const recent = useMemo(() => summary.data?.recent?.map(normalizePair) ?? [], [summary.data])
  const page = navigation.find(item => item.id === view)!
  const showQaExport = view !== 'questions' && view !== 'voc-next'
  const rangeLabel = filters.start || filters.end ? '自定义日期' : filters.days ? `近 ${filters.days} 天` : '全部时间'
  const scopeLabel = trend.data?.series.length ? `${trend.data.series[0].date} — ${trend.data.series.at(-1)!.date}` : rangeLabel

  const clearDrill = useCallback(() => {
    setDrillFilters(current => Object.keys(current).length ? {} : current)
    setDrillCustomerName('')
    setInitialReply('all')
  }, [])
  useEffect(() => {
    const handler = () => {
      setView(readView())
      setCustomerGroup(readCustomerGroup())
      clearDrill()
      setSelected(null)
      setTableExport(null)
      const topic = readTopicLink(location.hash)
      setLinkedTopic(topic?.id ?? null)
      setBriefingOpen(false)
      setJobsOpen(false)
      const scope = readChatFilters(location.hash) || topic?.filters
      if (scope) {
        const href = chatHref(scope)
        setFilters(current => chatHref(current) === href ? current : scope)
        setRange(scope.start || scope.end ? 'custom' : String(scope.days))
        setCustomStart(scope.start || '')
        setCustomEnd(scope.end || '')
      }
    }
    window.addEventListener('hashchange', handler)
    window.addEventListener('popstate', handler)
    return () => { window.removeEventListener('hashchange', handler); window.removeEventListener('popstate', handler) }
  }, [clearDrill])
  useEffect(() => { if (view === 'voc-chat' && location.hash !== chatHref(filters)) history.replaceState(null, '', chatHref(filters)) }, [view, filters])
  useEffect(() => { document.documentElement.classList.toggle('dark', theme === 'dark'); try { localStorage.setItem('qiyu-theme', theme) } catch { /* Storage may be unavailable. */ } }, [theme])
  useEffect(() => { document.documentElement.dataset.motion = motion ? 'on' : 'off' }, [motion])
  const navigate = useCallback((next: View, scope = filters, nextCustomerGroup = '') => {
    setCustomerGroup(nextCustomerGroup)
    setView(next)
    setSelected(null)
    setLinkedTopic(null)
    setBriefingOpen(false)
    setJobsOpen(false)
    if (next !== view) setTableExport(null)
    const href = next === 'voc-chat' ? chatHref(scope) : `#${next}`
    const state = nextCustomerGroup ? { customerGroup: nextCustomerGroup } : null
    if (location.hash !== href) history.pushState(state, '', href)
    else if (next === 'customers' && readCustomerGroup() !== nextCustomerGroup) history.replaceState(state, '', href)
  }, [filters, view])
  const clearLinkedTopic = useCallback(() => {
    setLinkedTopic(null)
    if (readTopicLink(location.hash)) history.replaceState(null, '', '#questions')
  }, [])
  const setChatScope = (next: ApiFilters) => { const scope = normalizeChatFilters(next); setFilters(scope); setRange(scope.start || scope.end ? 'custom' : String(scope.days)); setCustomStart(scope.start || ''); setCustomEnd(scope.end || ''); setDrillFilters({}) }
  const openChat = (next: ApiFilters) => { setChatScope(next); navigate('voc-chat', next) }
  const sideNavigate = useCallback((next: View) => { clearDrill(); navigate(next) }, [clearDrill, navigate])
  const drill = (next: Partial<ApiFilters> = {}, reply: Reply = 'all', customerName?: string) => { setDrillFilters(next); setDrillCustomerName(next.customer ? customerLabel(customerName, next.customer) : ''); setInitialReply(reply); navigate('pairs') }
  const openGroupCustomers = (group: string) => { clearDrill(); navigate('customers', filters, group) }
  const clearCustomerGroup = () => {
    if (!customerGroup) return
    setCustomerGroup('')
    if (readView() === 'customers') history.replaceState(null, '', location.href)
  }
  const changeGroup = (value: string) => {
    clearCustomerGroup()
    setFilters(current => ({ ...current, group: value === 'all' ? '' : value }))
    clearDrill()
  }
  const drillAudience = (next: Partial<ApiFilters>, reply: Reply = 'all', customerName?: string) =>
    drill({ ...(view === 'customers' && customerGroup ? { group: customerGroup } : {}), ...next }, reply, customerName)
  const openImport = useCallback(() => { if (!busy) setImportOpen(true) }, [busy])
  const changeDays = (days: number) => { setRange(String(days)); setFilters(current => ({ ...current, days, start: undefined, end: undefined })); setDrillFilters({}) }
  const reset = () => { clearCustomerGroup(); setFilters({ source, days: 7 }); setRange('7'); setCustomStart(''); setCustomEnd(''); setDrillFilters({}); setInitialReply('all') }
  const applyCustom = () => { if (!customStart && !customEnd) { toast.error('请至少填写一个日期边界'); return } if (customStart && customEnd && customStart > customEnd) { toast.error('开始日期不能晚于结束日期'); return } setFilters(current => ({ ...current, days: 0, start: customStart, end: customEnd })); setDrillFilters({}) }
  const cardNavigate = (next: string) => next === 'unanswered' ? drill({}, 'unanswered') : next === 'pairs' ? drill() : sideNavigate(next as View)
  const clean = async () => { setCleanOpen(false); setCleaning(true); try { const result = await apiPost<{ sessions_scanned: number; qa_pairs: number; clusters: number }>('clean', { source }); setDrillFilters({}); setSelected(null); setRefreshKey(key => key + 1); toast.success(`规则清洗完成：处理 ${result.sessions_scanned} 个会话，${result.qa_pairs} 条问答，${result.clusters} 个问题簇。`) } catch (error) { toast.error(error instanceof Error ? error.message : '清洗失败，请重试。') } finally { setCleaning(false) } }
  const exportCsv = async () => {
    const fallback = view === 'pairs' && initialReply !== 'all' ? { ...tableScope, replied: initialReply } : tableScope
    const exportScope = tableExport?.key === tableExportKey ? tableExport.filters : fallback
    setExporting(true)
    try { await downloadExport(exportScope); toast.success('已下载当前范围的脱敏问答') }
    catch (error) { toast.error(error instanceof Error ? error.message : '导出失败，请重试。') }
    finally { setExporting(false) }
  }
  const exportTemplate = async () => { if (downloadingTemplate) return; setDownloadingTemplate(true); try { await downloadTemplate(); toast.success('已下载会话导入模板') } catch (error) { toast.error(error instanceof Error ? error.message : '模板下载失败，请重试。') } finally { setDownloadingTemplate(false) } }

  return <SidebarProvider style={{ '--sidebar-width': 'calc(var(--spacing) * 64)', '--header-height': 'calc(var(--spacing) * 14)' } as CSSProperties}>
    <AppSidebar view={view} total={meta.data?.total} navigate={sideNavigate} onImport={openImport} />
    <SidebarInset data-density={compact ? 'compact' : 'normal'} data-motion={motion ? 'on' : 'off'}>
      <header className="relative flex h-(--header-height) shrink-0 items-center gap-2 border-b"><PageActivity /><div className="flex w-full items-center gap-1 px-4 lg:gap-2 lg:px-6"><SidebarTrigger className="-ml-1" /><Separator orientation="vertical" className="mx-2 data-[orientation=vertical]:h-4" /><h1 key={view} className="qiyu-page-title text-base font-medium">{page.title}</h1><div className="ml-auto flex items-center gap-2">
        <DropdownMenu><DropdownMenuTrigger asChild><Button variant="ghost" size="icon" aria-label="显示设置"><Settings2 className="size-4" /></Button></DropdownMenuTrigger><DropdownMenuContent align="end" className="w-48"><DropdownMenuLabel>显示设置</DropdownMenuLabel><DropdownMenuSeparator /><DropdownMenuCheckboxItem checked={motion} onCheckedChange={setMotion}>切换动画</DropdownMenuCheckboxItem><DropdownMenuCheckboxItem checked={compact} onCheckedChange={setCompact}>紧凑表格</DropdownMenuCheckboxItem><DropdownMenuSeparator /><DropdownMenuItem onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun /> : <Moon />}切换{theme === 'dark' ? '浅色' : '深色'}外观</DropdownMenuItem></DropdownMenuContent></DropdownMenu>
        {view === 'cleaning' && <VocJobHistory open={jobsOpen} onOpenChange={setJobsOpen} trigger={<Button variant="outline" size="sm"><History />模型整理记录</Button>} />}
        {view !== 'settings' && view !== 'voc-chat' && view !== 'voc-next' && <Button size="sm" onClick={openImport} disabled={busy}><CirclePlus /><span className="hidden sm:inline">导入会话</span></Button>}
      </div></div></header>
      <div className="flex flex-1 flex-col"><div className="@container/main flex flex-1 flex-col gap-2"><div className={`flex flex-col ${view === 'voc-chat' ? 'min-h-0 gap-3 h-[calc(100svh-var(--header-height))] overflow-hidden md:h-[calc(100svh-var(--header-height)-1rem)]' : 'gap-4 py-4 md:gap-6 md:py-6'}`}>
        {meta.error && view !== 'settings' && <div className="px-4 lg:px-6"><QueryState error={meta.error} retry={meta.retry} /></div>}
        {view === 'clusters' && <div className="px-4 lg:px-6">
          <Button variant="ghost" size="sm" onClick={() => navigate('cleaning')}><ArrowLeft />返回数据清洗</Button>
        </div>}
        {view !== 'cleaning' && view !== 'settings' && <div className={`flex shrink-0 flex-wrap items-center gap-2 px-4 lg:px-6 ${view === 'voc-chat' ? 'py-3' : ''}`}>
          <div className="mr-auto hidden items-center gap-2 text-sm text-muted-foreground xl:flex"><CalendarDays className="size-4" />{scopeLabel}</div>
          <Select value={activeGroup || 'all'} onValueChange={changeGroup}><SelectTrigger size="sm" className="w-[156px]" aria-label="客户群筛选"><UsersRound className="size-3.5" /><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部客户群</SelectItem>{activeGroup && !meta.data?.groups.includes(activeGroup) && <SelectItem value={activeGroup}>{activeGroup}</SelectItem>}{meta.data?.groups.map(group => <SelectItem key={group} value={group}>{group}</SelectItem>)}</SelectContent></Select>
          <Select value={filters.staff || 'all'} onValueChange={value => { setFilters(current => ({ ...current, staff: value === 'all' ? '' : value })); setDrillFilters({}) }}><SelectTrigger size="sm" className="w-[134px]" aria-label="客服筛选"><ContactRound className="size-3.5" /><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部客服</SelectItem>{meta.data?.staff.map(staff => <SelectItem key={staff} value={staff}>{staff}</SelectItem>)}</SelectContent></Select>
          <Select value={range} onValueChange={value => { setRange(value); if (value !== 'custom') changeDays(Number(value)) }}><SelectTrigger size="sm" className="w-[128px]" aria-label="时间筛选"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="7">最近 7 天</SelectItem><SelectItem value="30">最近 30 天</SelectItem><SelectItem value="90">最近 90 天</SelectItem><SelectItem value="0">全部时间</SelectItem><SelectItem value="custom">自定义日期</SelectItem></SelectContent></Select>
          <Button variant="ghost" size="sm" onClick={reset}>重置</Button>{view !== 'voc-chat' && <><RefreshDataButton onRefresh={() => setRefreshKey(key => key + 1)} />{showQaExport && <Button variant="outline" size="sm" onClick={exportCsv} disabled={exporting} aria-busy={exporting}>{exporting ? <LoaderCircle className="size-3.5 animate-spin" /> : <ArrowDownToLine className="size-3.5" />}<span className="hidden sm:inline">导出脱敏问答</span></Button>}</>}
          {(view === 'questions' || view === 'voc-next') && <VocBriefingDialog open={briefingOpen} onOpenChange={setBriefingOpen} filters={sharedScope} defaultKind={view === 'voc-next' ? 'sops' : 'topics'} trigger={<Button variant="outline" size="sm"><FileText />飞书成果预览</Button>} />}
        </div>}
        {range === 'custom' && view !== 'cleaning' && view !== 'settings' && <form className="flex flex-wrap items-center gap-3 px-4 text-sm lg:px-6" onSubmit={event => { event.preventDefault(); applyCustom() }}><label className="flex items-center gap-2">开始日期<Input type="date" value={customStart} onChange={event => setCustomStart(event.target.value)} min="2000-01-01" max="2100-12-31" className="w-40" /></label><label className="flex items-center gap-2">结束日期<Input type="date" value={customEnd} onChange={event => setCustomEnd(event.target.value)} min="2000-01-01" max="2100-12-31" className="w-40" /></label><Button size="sm" type="submit">应用日期</Button></form>}
        {view === 'overview' && <>
          <QiyuSectionCards summary={summary.data} rangeLabel={rangeLabel} loading={summary.loading} onNavigate={cardNavigate} />
          {summary.error && <div className="px-4 lg:px-6"><QueryState error={summary.error} retry={summary.retry} /></div>}
          <div className="px-4 lg:px-6">{trend.error ? <QueryState error={trend.error} retry={trend.retry} /> : <QiyuAreaChart data={trend.data} loading={trend.loading} days={filters.start || filters.end ? -1 : filters.days ?? 0} onDaysChange={changeDays} onDaySelect={day => drill({ days: 0, start: day, end: day })} animate={motion} />}</div>
          <div className="px-4 lg:px-6"><QATable key={`overview-${source}`} data={recent} remote={{ filters: sharedScope, refreshKey }} onSelect={openPair} onFiltersChange={captureTableFilters} /></div>
        </>}
        {view === 'pairs' && <div className="space-y-4 px-4 lg:px-6">{Object.values(drillFilters).some(value => value !== undefined && value !== '') && <div className="flex flex-wrap items-center gap-2"><span className="text-sm text-muted-foreground">限定条件</span>{drillFilters.cluster && <Badge variant="secondary">问题簇 {drillFilters.cluster}</Badge>}{drillFilters.customer && <Badge variant="secondary">{drillCustomerName || customerLabel(undefined, drillFilters.customer)}</Badge>}{drillFilters.group && <Badge variant="secondary">{drillFilters.group}</Badge>}{drillFilters.start && <Badge variant="secondary">{drillFilters.start}{drillFilters.end !== drillFilters.start ? ` 至 ${drillFilters.end || '不限'}` : ''}</Badge>}<Button variant="ghost" size="xs" onClick={clearDrill}>清除限定条件</Button></div>}<QATable key={`${source}-${JSON.stringify(drillFilters)}-${initialReply}`} data={[]} remote={{ filters: effectiveFilters, refreshKey }} onSelect={openPair} initialReply={initialReply} onFiltersChange={captureTableFilters} /></div>}
        {view === 'settings' && <div className="px-4 lg:px-6"><GatewaySettings onSaved={() => setRefreshKey(key => key + 1)} /></div>}
        {view === 'questions' && <div className="px-4 lg:px-6"><VocFrequencyWorkspace key={linkedTopic ?? 'ranking'} initialTopic={linkedTopic} onCloseLinkedTopic={clearLinkedTopic} filters={sharedScope} refreshKey={refreshKey} onOpenPair={(pair, anchorMessageId) => openPair(pair, anchorMessageId, true)} onOpenChat={openChat} /></div>}
        {view === 'voc-next' && <div className="px-4 lg:px-6"><VocKnowledgeLibrary filters={sharedScope} refreshKey={refreshKey} onOpenPair={openPair} /></div>}
        {view === 'voc-chat' && <VocChatWorkspace filters={filters} refreshKey={refreshKey} onScopeChange={setChatScope} />}
        {view === 'clusters' && <div className="px-4 lg:px-6">
          <RuleClusterWorkspace filters={sharedScope} refreshKey={refreshKey} onDrill={drill} />
        </div>}
        {(view === 'groups' || view === 'customers') && <div className="px-4 lg:px-6">
          <AudienceWorkspace key={view} kind={view} filters={view === 'customers' ? audienceScope : sharedScope} refreshKey={refreshKey}
            scopedGroup={view === 'customers' ? customerGroup : undefined} onClearScopedGroup={() => changeGroup('all')}
            onViewCustomers={openGroupCustomers} onDrill={drillAudience} />
        </div>}
        {view === 'cleaning' && <div className="space-y-6 px-4 lg:px-6"><div className="grid gap-4 lg:grid-cols-2"><Card><CardHeader><FileSpreadsheet className="mb-3 size-6" /><CardTitle>导入会话数据</CardTitle><CardDescription>上传 CSV / XLSX，先检查字段和脱敏样本，确认后写入业务库并执行规则清洗。</CardDescription></CardHeader><CardFooter className="gap-3"><Button onClick={openImport} disabled={busy}><Upload />选择文件并预览</Button><Button variant="outline" onClick={() => void exportTemplate()} disabled={downloadingTemplate} aria-busy={downloadingTemplate}>{downloadingTemplate && <LoaderCircle className="size-3.5 animate-spin" />}下载模板</Button></CardFooter></Card><Card><CardHeader><ListFilter className="mb-3 size-6" /><CardTitle>重新执行规则清洗</CardTitle><CardDescription>重新抽取业务库保留的会话，配对问答并重算问题簇。不会调用模型。</CardDescription></CardHeader><CardFooter className="flex-wrap gap-2"><Button variant="outline" onClick={() => setCleanOpen(true)} disabled={busy || !status.data?.sessions}>{cleaning ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}{cleaning ? '正在规则清洗' : '重新规则清洗'}</Button>
          <Button variant="outline" onClick={() => navigate('clusters')}><Layers3 />核对问法归类</Button>
        </CardFooter></Card></div>
          <QueryState loading={status.loading} error={status.error} retry={status.retry}><div className="grid gap-4 lg:grid-cols-[1fr_1.6fr]"><Card><CardHeader><CardTitle>处理状态</CardTitle><CardDescription>当前数据源全库统计，不受分析页筛选影响。</CardDescription></CardHeader><CardContent><dl className="space-y-3 text-sm">{[['原始会话', 'sessions'], ['有正文的会话', 'with_content'], ['已处理', 'processed'], ['待规则清洗', 'pending'], ['抽取到问答', 'with_pairs'], ['正文无有效问题', 'no_question'], ['缺少正文', 'no_content'], ['正文已归档', 'archived'], ['有效问答', 'total_pairs']].map(([label, key]) => <div key={key} className="flex justify-between gap-3"><dt className="text-muted-foreground">{label}</dt><dd className="tabular-nums">{numberText(status.data?.[key])}</dd></div>)}</dl></CardContent><CardFooter className="text-xs text-muted-foreground">规则版本 {status.data?.version || '—'}</CardFooter></Card><Card><CardHeader><CardTitle>最近处理记录</CardTitle><CardDescription>当前数据源的导入与规则清洗结果。</CardDescription></CardHeader><CardContent>{status.data?.tasks.length ? <div className="overflow-x-auto"><Table><TableHeader><TableRow><TableHead>时间</TableHead><TableHead>操作</TableHead><TableHead>摘要</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>{status.data.tasks.map((task, index) => <TableRow key={`${task.created_at_text}-${index}`}><TableCell className="whitespace-nowrap">{task.created_at_text}</TableCell><TableCell className="whitespace-nowrap">{task.kind === 'local_import' ? '导入并清洗' : '规则清洗'}</TableCell><TableCell className="min-w-48 text-sm text-muted-foreground">{task.kind === 'local_import' ? `${String(task.result.filename || '文件')}：新增 ${task.result.new_sessions ?? 0} / 更新 ${task.result.updated_sessions ?? 0} 个会话；跳过错误行 ${task.result.skipped_rows ?? 0}` : `处理 ${task.result.sessions_scanned ?? 0} 个会话，${task.result.qa_pairs ?? 0} 条问答`}</TableCell><TableCell><Badge variant="outline">{task.status === 'success' || task.status === 'ok' ? '完成' : task.status || '已记录'}</Badge></TableCell></TableRow>)}</TableBody></Table></div> : <p className="py-8 text-center text-sm text-muted-foreground">暂无本地处理记录</p>}</CardContent></Card></div></QueryState>
        </div>}
        {view !== 'voc-chat' && <footer className="flex items-center justify-between gap-4 px-4 pt-1 text-xs text-muted-foreground lg:px-6"><span className="flex items-center gap-1.5"><ShieldCheck className="size-3.5" />{view === 'settings' ? '仅管理员可管理模型服务配置' : '问答与会话内容按服务端规则脱敏'}</span><span>{meta.data?.db_name || '语海Voc'}</span></footer>}
      </div></div></div>
    </SidebarInset>
    <QADetail pair={selected?.pair ?? null} anchorMessageId={selected?.anchorMessageId} sessionContext={selected?.sessionContext} source={source} onClose={() => setSelected(null)} onCloseAutoFocus={restorePairFocus} />
    <ImportDialog open={importOpen} onOpenChange={setImportOpen} onImported={() => { setFilters({ source: 'local', days: 0 }); setRange('0'); setDrillFilters({}); setSelected(null); setRefreshKey(key => key + 1); navigate('cleaning') }} />
    <AlertDialog open={cleanOpen} onOpenChange={setCleanOpen}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>重新执行业务库规则清洗？</AlertDialogTitle><AlertDialogDescription>将重新抽取当前库保留的会话并重算问题簇，簇编号可能变化。原始正文不会删除，没有正文的归档问答会保留。不调用模型或外部服务。</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction onClick={() => void clean()}>确认重新清洗</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
    <Toaster position="top-center" theme={theme} />
  </SidebarProvider>
}

export default function QiyuApp() { return <Workspace /> }

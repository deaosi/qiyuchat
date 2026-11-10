import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { ArrowDown, ArrowUp, Check, Copy, History, MoreHorizontal, Plus, Square, SquarePen, Trash2, X } from 'lucide-react'
import { toast } from 'sonner'
import { useApi } from '@/hooks/use-api'
import { type ApiFilters, type GatewayConfig } from '@/lib/api'
import { chatHref, chatScopeKey, chatScopeLabel, citationFilters, getVocChatStore, type ChatCitation, type ChatMessage, type ChatThread, type VocChatStore } from '@/lib/voc-chat'
import type { QAPair } from '@/lib/demo-data'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { EvidenceList, KnowledgeContent, QueryPanel, type Evidence, type Knowledge, type TopicDetail } from './voc-next-workspace'
import { LoadingMark, LoadingState, LoadingValue } from './loading-state'

type Selection = { threadId: string; citation: ChatCitation; message: ChatMessage; filters: ApiFilters }
type Coverage = { source_pairs: number; ready_knowledge: number; auto_reusable_knowledge: number; coverage: { processed_pairs: number; pending_pairs: number } }
const suggestions = [
  { title: '发现高频问题', description: '看客户正在集中咨询什么', query: '当前范围的高频咨询有哪些？' },
  { title: '查找可复用答案', description: '保留适用条件与验证状态', query: '查一下知汇鸽配置和排查知识，保留适用条件和会话验证状态。' },
  { title: '查看客服原话', description: '从实际接待中找到回复依据', query: '客服通常怎么回复场景未触发的问题？请提供原话依据。' },
  { title: '了解整理覆盖', description: '区分来源知识与已验证答案', query: '当前已整理多少条、待整理多少条，有多少答案通过会话验证可直接复用？' },
]
const amount = (value?: number) => typeof value === 'number' ? value.toLocaleString('zh-CN') : '—'
const timestamp = (value: number) => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(value)

function useMedia(query: string) {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const media = window.matchMedia(query), change = () => setMatches(media.matches)
    change(); media.addEventListener('change', change)
    return () => media.removeEventListener('change', change)
  }, [query])
  return matches
}

function SourceInspector({ selection, onOpenPair }: { selection: Selection; onOpenPair: (pair: QAPair) => void }) {
  const { citation, filters } = selection
  const topic = useApi<TopicDetail>('voc2/topic', { ...filters, id: citation.id }, 0, citation.kind === 'topic')
  const knowledge = useApi<{ item: Knowledge }>('voc2/knowledge', { ...filters, id: citation.id }, 0, citation.kind === 'knowledge')
  const event = useApi<{ event: Evidence }>('voc2/event', { ...filters, id: citation.id }, 0, citation.kind === 'event')
  const query = citation.kind === 'topic' ? topic : citation.kind === 'knowledge' ? knowledge : event
  return <div className="space-y-5">
    <div className="space-y-2"><p className="text-xs text-muted-foreground">{citation.kind === 'topic' ? '问题主题' : citation.kind === 'knowledge' ? '知识与验证' : '会话事件'}</p><h3 className="text-base font-semibold leading-6">{citation.title}</h3><p className="text-xs leading-5 text-muted-foreground">查询范围：{chatScopeLabel(filters)}</p></div>
    <QueryPanel hasData={query.data !== null} loading={query.loading} error={query.error} retry={query.retry}>
      {citation.kind === 'topic' && topic.data && <><p className="text-sm leading-6 text-muted-foreground">代表问法：{topic.data.topic.question}</p><EvidenceList evidence={topic.data.events} onOpenPair={onOpenPair} /></>}
      {citation.kind === 'knowledge' && knowledge.data && <KnowledgeContent item={knowledge.data.item} onOpenPair={onOpenPair} />}
      {citation.kind === 'event' && event.data && <EvidenceList evidence={[event.data.event]} onOpenPair={onOpenPair} />}
    </QueryPanel>
  </div>
}

export function VocChatWorkspace({ filters, refreshKey = 0, onScopeChange, onOpenPair, store = getVocChatStore() }: { filters: ApiFilters; refreshKey?: number; onScopeChange: (filters: ApiFilters) => void; onOpenPair: (pair: QAPair) => void; store?: VocChatStore }) {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot)
  const scopeKey = chatScopeKey(filters)
  const thread = state.threads.find(t => t.id === state.activeId)
  const current = thread && chatScopeKey(thread.filters) === scopeKey ? thread : undefined
  const pending = !!(current && state.pending[current.id])
  const [historyOpen, setHistoryOpen] = useState(false)
  const [sourceSelection, setSelection] = useState<Selection | null>(null)
  const selection = sourceSelection?.threadId === current?.id ? sourceSelection : null
  const [deleting, setDeleting] = useState<ChatThread | null>(null)
  const [atBottom, setAtBottom] = useState(true)
  const [copied, setCopied] = useState('')
  const historyWide = useMedia('(min-width: 1280px)')
  const sourcesWide = useMedia('(min-width: 1536px)')
  const scroll = useRef<HTMLDivElement>(null), input = useRef<HTMLTextAreaElement>(null), previousThread = useRef('')
  const nearBottom = useRef(true)
  const gateway = useApi<GatewayConfig>('gateway/config', { source: 'local' }, refreshKey)
  const coverage = useApi<Coverage>('voc2/summary', current?.filters || filters, refreshKey, !!current)

  useEffect(() => { store.ensureScope(filters) }, [scopeKey, store])
  useEffect(() => { setSelection(null) }, [state.activeId])
  useEffect(() => { if (historyWide) setHistoryOpen(false) }, [historyWide])
  useEffect(() => {
    const area = input.current
    if (area) { area.style.height = 'auto'; area.style.height = `${Math.min(area.scrollHeight, 160)}px` }
  }, [current?.draft])
  useEffect(() => {
    const panel = scroll.current
    if (!panel || !current) return
    if (previousThread.current !== current.id || nearBottom.current) {
      panel.scrollTop = panel.scrollHeight; nearBottom.current = true; setAtBottom(true)
    }
    previousThread.current = current.id
  }, [current?.id, current?.messages.length, pending])

  function selectThread(id: string) {
    const selected = store.select(id)
    if (selected) onScopeChange(selected.filters)
    setHistoryOpen(false)
  }
  function newThread() {
    store.newThread(current?.filters || filters); setSelection(null); setHistoryOpen(false)
    requestAnimationFrame(() => input.current?.focus())
  }
  function send() {
    if (!current || pending || !current.draft.trim()) return
    nearBottom.current = true
    void store.send(current.id, current.draft)
  }
  function openSource(citation: ChatCitation, message: ChatMessage) {
    if (!current) return
    setSelection({ threadId: current.id, citation, message, filters: citationFilters(current.filters, message.queryFilters) })
  }
  async function copy(message: ChatMessage) {
    try { await navigator.clipboard.writeText(message.content); setCopied(message.id); window.setTimeout(() => setCopied(''), 1800) }
    catch { toast.error('未能复制，请选中文字复制。') }
  }
  const history = <div className="flex min-h-0 flex-1 flex-col">
    <div className="p-4"><Button variant="outline" className="w-full justify-start gap-2 bg-background shadow-none" onClick={newThread}><SquarePen className="size-4" />新建对话</Button></div>
    <div className="px-4 pb-2 text-[11px] font-medium tracking-wide text-muted-foreground">本窗口的对话</div>
    <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-4">{state.threads.map(item => <div key={item.id} className={`group flex items-center rounded-lg ${item.id === current?.id ? 'bg-muted' : 'hover:bg-muted/60'}`}>
      <button className="min-w-0 flex-1 px-3 py-3 text-left" onClick={() => selectThread(item.id)}><span className="flex items-center gap-2 text-sm"><span className="truncate">{item.title}</span>{state.pending[item.id] && <LoadingMark className="size-3 text-muted-foreground" />}</span><span className="mt-1.5 block text-[11px] text-muted-foreground">{timestamp(item.updatedAt)}</span></button>
      <DropdownMenu><DropdownMenuTrigger asChild><Button variant="ghost" size="icon-sm" aria-label={`管理对话：${item.title}`} className="mr-1 shrink-0 opacity-50 group-hover:opacity-100 focus:opacity-100"><MoreHorizontal className="size-4" /></Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem onClick={() => setDeleting(item)}><Trash2 />删除本窗口对话</DropdownMenuItem></DropdownMenuContent></DropdownMenu>
    </div>)}</div>
    <p className="border-t px-4 py-3 text-[11px] leading-5 text-muted-foreground">最近 20 个对话保存在此标签页中，刷新后可继续。</p>
  </div>

  return <section className="flex min-h-0 flex-1 overflow-hidden" aria-label="独立数据聊天窗口">
    <aside className="hidden w-56 shrink-0 flex-col border-r bg-muted/20 xl:flex" aria-label="对话记录">{history}</aside>
    <div className="relative flex min-w-0 flex-1 flex-col bg-transparent">
      <div className="flex min-h-16 shrink-0 items-center gap-3 border-b bg-background/80 px-4 md:px-6">
        <Button variant="ghost" size="icon-sm" className="xl:hidden" aria-label="打开对话记录" onClick={() => setHistoryOpen(true)}><History /></Button>
        <div className="min-w-0 flex-1"><div className="flex items-center gap-2"><h2 className="truncate text-sm font-semibold">{current?.messages.length ? current.title : '问问数据'}</h2></div><p className="mt-1 truncate text-xs text-muted-foreground" title={chatScopeLabel(current?.filters || filters)}>{chatScopeLabel(current?.filters || filters)}</p></div>
        <Button variant="ghost" size="icon-sm" aria-label="新建对话" className="hidden sm:inline-flex xl:hidden" onClick={newThread}><Plus /></Button>
        <Button variant="ghost" size="sm" className="px-2 text-xs" asChild><a href={chatHref(current?.filters || filters)} target="_blank" rel="noopener noreferrer" aria-label="在新标签打开聊天窗口">新标签</a></Button>
        <Button variant="ghost" size="sm" className="px-2 text-xs" aria-label="查看当前回答依据" onClick={() => {
          const message = [...(current?.messages || [])].reverse().find(m => m.citations?.length)
          if (message?.citations?.[0]) openSource(message.citations[0], message)
          else toast.info('回答附有来源后，可在这里查看原话与适用条件。')
        }}>依据</Button>
      </div>

      <div ref={scroll} className="min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]" onScroll={() => {
        const panel = scroll.current
        if (panel) { const next = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 100; nearBottom.current = next; setAtBottom(next) }
      }} aria-label="聊天消息">
        {!current ? <LoadingState label="正在打开对话…" className="mx-auto h-full max-w-3xl px-6 md:px-10" /> : current.messages.length === 0 ? <div key={current.id} className="qiyu-page-enter mx-auto flex min-h-full w-full max-w-3xl flex-col justify-center px-6 py-10 md:px-10">
          <p className="mb-3 text-xs text-muted-foreground">知汇鸽使用支持</p>
          <h3 className="text-xl font-semibold tracking-tight">查询会话数据</h3>
          <p className="mt-3 max-w-lg text-sm leading-7 text-muted-foreground">查看高频咨询、接待回复和知识依据。</p>
          <div className="mt-7 max-w-xl divide-y border-y">{suggestions.map(item => <button key={item.title} onClick={() => { store.setDraft(current.id, item.query); input.current?.focus() }} className="flex w-full flex-wrap items-center justify-between gap-1 px-1 py-3.5 text-left transition-colors duration-150 hover:bg-muted/30"><span className="text-sm font-medium">{item.title}</span><span className="text-xs leading-5 text-muted-foreground">{item.description}</span></button>)}</div>
        </div> : <div key={current.id} className="qiyu-page-enter mx-auto w-full max-w-3xl space-y-8 px-5 py-8 md:px-9" role="log" aria-label="对话内容" aria-live="polite">
          {current.messages.map(message => <article key={message.id} className={message.role === 'user' ? 'flex justify-end' : 'space-y-3'}>
            {message.role === 'user' ? <p className="max-w-[88%] whitespace-pre-wrap break-words rounded-lg bg-muted/70 px-4 py-3 text-sm leading-7">{message.content}</p> : <>
              <div className="flex items-center gap-2 text-xs font-medium">查询结果{message.model && <span className="ml-auto truncate text-[11px] font-normal text-muted-foreground">{message.model}</span>}</div>
              <div className="whitespace-pre-wrap break-words text-sm leading-7 [overflow-wrap:anywhere]">{message.content}</div>
              {!!message.citations?.length && <div className="space-y-2 border-t pt-3"><div className="text-[11px] font-medium text-muted-foreground">回答依据（{message.citations.length}）</div>{message.citations.map((citation, index) => <button key={`${citation.kind}-${citation.id}`} onClick={() => openSource(citation, message)} className={`flex w-full items-start gap-2 py-1.5 text-left text-xs leading-5 underline-offset-4 transition-colors duration-150 hover:text-foreground hover:underline ${selection?.message.id === message.id && selection.citation.id === citation.id ? 'text-foreground underline' : 'text-muted-foreground'}`}><span className="shrink-0 tabular-nums">{index + 1}.</span><span className="min-w-0 flex-1 break-words">{citation.title || `来源 ${citation.id}`}</span></button>)}</div>}
              <div className="flex items-start justify-between gap-3"><p className="text-[11px] leading-5 text-muted-foreground">{[message.scope, message.tools?.map(t => t.label).join('、')].filter(Boolean).join(' · ')}</p><Button size="icon-sm" variant="ghost" aria-label="复制回答" onClick={() => void copy(message)}>{copied === message.id ? <Check className="size-3.5" /> : <Copy className="size-3.5 text-muted-foreground" />}</Button></div>
            </>}
          </article>)}
          {pending && <LoadingState label="正在查询会话与知识依据…" rows={2} className="min-h-28 py-2" />}
        </div>}
      </div>
      {!atBottom && current?.messages.length ? <Button variant="outline" size="icon-sm" className="absolute bottom-40 right-6 rounded-full bg-background shadow-sm" aria-label="回到最新消息" onClick={() => { if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; nearBottom.current = true; setAtBottom(true) }}><ArrowDown /></Button> : null}

      <div className="shrink-0 px-4 pb-4 pt-2 md:px-8 md:pb-5"><div className="mx-auto max-w-3xl">
        {state.storageWarning && <p role="status" className="mb-2 text-xs text-muted-foreground">本窗口暂时无法保存聊天历史，刷新后可能无法恢复当前对话。</p>}
        {current?.error && <p role="alert" className="mb-2 text-sm text-destructive">{current.error}</p>}
        <form onSubmit={event => { event.preventDefault(); send() }} className="qiyu-surface rounded-xl border border-input p-3 transition-[border-color,box-shadow] duration-150 focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/30">
          <textarea ref={input} rows={2} aria-label="向语海Voc提问" placeholder="询问高频问题、接待答复或排查依据…" maxLength={2000} value={current?.draft || ''} disabled={!current || pending} onChange={event => current && store.setDraft(current.id, event.target.value)} onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); send() }
          }} className="block min-h-14 max-h-40 w-full resize-none bg-transparent px-1 py-1 text-sm leading-6 outline-none placeholder:text-muted-foreground/70 disabled:opacity-60" />
          <div className="mt-2 flex items-center gap-3"><span className="max-w-[55%] truncate px-1 py-1 text-[11px] text-muted-foreground" title={gateway.data?.model}>{gateway.data?.model || (gateway.loading ? '正在读取模型' : '使用已配置模型')}</span><span className="ml-auto hidden text-[10px] text-muted-foreground/70 sm:inline">Enter 发送 · Shift+Enter 换行</span>{pending ? <Button type="button" size="icon-sm" aria-label="停止等待" title="停止等待" onClick={() => current && store.stop(current.id)}><Square className="size-3.5 fill-current" /></Button> : <Button type="submit" size="icon-sm" className="rounded-lg" aria-label="发送问题" disabled={!current?.draft.trim()}><ArrowUp className="size-4" /></Button>}</div>
        </form>

      </div></div>
    </div>

    {sourcesWide && <aside className="flex w-[340px] shrink-0 flex-col border-l bg-muted/10" aria-label="回答来源面板"><div className="flex h-16 shrink-0 items-center justify-between border-b px-5"><h2 className="text-sm font-medium">回答依据</h2>{selection && <Button variant="ghost" size="icon-sm" aria-label="关闭来源" onClick={() => setSelection(null)}><X className="size-4" /></Button>}</div><div className="min-h-0 flex-1 overflow-y-auto p-5">{selection ? <SourceInspector selection={selection} onOpenPair={onOpenPair} /> : <div className="space-y-6"><div><h3 className="text-sm font-medium">查看引用来源</h3><p className="mt-2 text-xs leading-6 text-muted-foreground">点击回答下的来源，查看对应会话、适用条件和后续反馈。</p></div><div className="space-y-3 border-t pt-5 text-xs"><p className="font-medium">当前范围</p><p className="leading-6 text-muted-foreground">{chatScopeLabel(current?.filters || filters)}</p>{[['来源问答', coverage.data?.source_pairs], ['已整理来源', coverage.data?.coverage.processed_pairs], ['来源知识', coverage.data?.ready_knowledge], ['会话验证答案', coverage.data?.auto_reusable_knowledge]].map(([label, value]) => <div key={String(label)} className="flex justify-between gap-3"><span className="text-muted-foreground">{label}</span><span className="tabular-nums">{coverage.loading ? <LoadingValue className="h-3 w-8 rounded-sm" /> : amount(value as number | undefined)}</span></div>)}</div></div>}</div></aside>}

    <Sheet open={historyOpen && !historyWide} onOpenChange={setHistoryOpen}><SheetContent side="left" className="w-72 gap-0 p-0"><SheetHeader className="border-b p-4"><SheetTitle>对话记录</SheetTitle><SheetDescription>本窗口保留的最近对话</SheetDescription></SheetHeader>{history}</SheetContent></Sheet>
    <Sheet open={!!selection && !sourcesWide} onOpenChange={open => { if (!open) setSelection(null) }}><SheetContent className="w-full overflow-y-auto sm:max-w-2xl"><SheetHeader><SheetTitle>回答依据</SheetTitle><SheetDescription>按这条回答的实际查询范围读取原始依据。</SheetDescription></SheetHeader><div className="px-4 pb-6">{selection && !sourcesWide && <SourceInspector selection={selection} onOpenPair={onOpenPair} />}</div></SheetContent></Sheet>
    <AlertDialog open={!!deleting} onOpenChange={open => { if (!open) setDeleting(null) }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>删除本窗口对话？</AlertDialogTitle><AlertDialogDescription>将删除本窗口保存的“{deleting?.title}”。</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction onClick={() => { if (deleting) { const next = store.remove(deleting.id); onScopeChange(next.filters) } setDeleting(null) }}>删除对话</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </section>
}

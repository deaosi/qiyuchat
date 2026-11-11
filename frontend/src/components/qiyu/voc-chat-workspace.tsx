import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { ArrowDown, ArrowUp, Check, Copy, History, Square, SquarePen, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { useApi } from '@/hooks/use-api'
import { type ApiFilters, type GatewayConfig } from '@/lib/api'
import { chatScopeKey, chatScopeLabel, citationFilters, getVocChatStore, type ChatCitation, type ChatMessage, type ChatThread, type VocChatStore } from '@/lib/voc-chat'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { VocSourceInspector } from './voc-source-inspector'
import { LoadingMark, LoadingState } from './loading-state'

type Selection = { threadId: string; citation: ChatCitation; message: ChatMessage; filters: ApiFilters }
type Preset = { prompt: string; keywords: string[] }
const draftLimit = 2000
const suggestions: Preset[] = [
  { prompt: '当前范围的高频咨询有哪些？', keywords: ['高频问题', '高频咨询', '查高频'] },
  { prompt: '客服通常怎么回复场景未触发的问题？', keywords: ['话术', '客服回复', '接待答复', '场景未触发'] },
  { prompt: '哪些答案已通过会话验证，可以直接复用？', keywords: ['知识库', '可复用答案', '验证结果'] },
  { prompt: '机器人不回复时应先检查哪些设置？请附上案例依据。', keywords: ['排查', '机器人不回复', '功能异常'] },
]
const topicSuggestions: Preset[] = [
  { prompt: '这个问题怎么排查？给出适用条件和来源。', keywords: ['排查', '排查依据'] },
  { prompt: '这个问题有哪些可以直接复用的客服答复？', keywords: ['话术', '客服回复', '知识库', '可复用答案'] },
  { prompt: '只给数字，比较这个问题本期和上期的咨询量。', keywords: ['趋势', '对比', '咨询量', '高频问题'] },
]

function matchPreset(draft: string, presets: Preset[]) {
  const prefix = draft.trim()
  if (prefix.length < 2 || /[\r\n]/.test(draft) || presets.some(({ prompt }) => prompt === prefix)) return
  const matches = presets.filter(({ prompt, keywords }) => prompt.startsWith(prefix) || keywords.some(word => word.startsWith(prefix)))
  if (matches.length === 1) return matches[0].prompt
}

const timestamp = (value: number) => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(value)

export function VocChatWorkspace({ filters, refreshKey = 0, onScopeChange, store = getVocChatStore() }: { filters: ApiFilters; refreshKey?: number; onScopeChange: (filters: ApiFilters) => void; store?: VocChatStore }) {
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
  const [dismissedCompletion, setDismissedCompletion] = useState<{ threadId: string; draft: string } | null>(null)
  const scroll = useRef<HTMLDivElement>(null), input = useRef<HTMLTextAreaElement>(null), previousThread = useRef('')
  const nearBottom = useRef(true)
  const completionId = useId()
  const gateway = useApi<GatewayConfig>('gateway/config', { source: 'local' }, refreshKey)
  const focus = useApi<{ topic: { question: string } }>('voc2/topic', { ...filters, id: filters.focus_topic }, refreshKey, Boolean(filters.focus_topic))
  const topicTitle = filters.focus_topic ? focus.data?.topic.question || '当前问题' : ''
  const presets = filters.focus_topic ? topicSuggestions : suggestions
  const completionDismissed = current && dismissedCompletion?.threadId === current.id && dismissedCompletion.draft === current.draft
  const completion = current && !pending && !completionDismissed ? matchPreset(current.draft, presets) : undefined

  useEffect(() => { store.ensureScope(filters) }, [scopeKey, store])
  useEffect(() => { setSelection(null) }, [state.activeId])
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
  function fillPreset(prompt: string) {
    if (!current || pending) return
    const draft = current.draft.trim() ? `${current.draft}${current.draft.endsWith('\n') ? '' : '\n'}${prompt}` : prompt
    if (draft.length > draftLimit) {
      toast.error('草稿已接近 2000 字，请删减后再加入话术。')
      return
    }
    store.setDraft(current.id, draft)
  }
  function acceptCompletion() {
    if (!current || !completion) return
    store.setDraft(current.id, completion)
    input.current?.focus()
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
    <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 py-3">{state.threads.map(item => <div key={item.id} className={`group flex items-center rounded-lg transition-colors duration-150 ${item.id === current?.id ? 'bg-muted' : 'hover:bg-muted/60'}`}>
      <button className="min-w-0 flex-1 px-3 py-3 text-left" onClick={() => selectThread(item.id)}><span className="flex items-center gap-2 text-sm"><span className="truncate">{item.title}</span>{state.pending[item.id] && <LoadingMark className="size-3 text-muted-foreground" />}</span><span className="mt-1.5 block text-[11px] text-muted-foreground">{timestamp(item.updatedAt)}</span></button>
      <Button variant="ghost" size="icon-sm" aria-label={`删除对话：${item.title}`} className="mr-1 shrink-0 text-muted-foreground hover:text-foreground" onClick={() => { setHistoryOpen(false); setDeleting(item) }}><Trash2 className="size-3.5" /></Button>
    </div>)}</div>
    <p className="border-t px-4 py-3 text-[11px] leading-5 text-muted-foreground">最近 20 个对话保存在此标签页中，刷新后可继续。</p>
  </div>

  return <section className="qiyu-surface relative mx-4 mb-4 flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border lg:mx-6 lg:mb-6" aria-label="独立数据聊天窗口">
      <div className="flex min-h-14 shrink-0 items-center gap-3 border-b px-3 md:px-5">
        <h2 className="min-w-0 flex-1 truncate text-sm font-medium text-muted-foreground" title={topicTitle || current?.title}>{topicTitle || (current?.messages.length ? current.title : '新对话')}</h2>
        {filters.focus_topic && <Button variant="ghost" size="sm" onClick={() => onScopeChange({ ...filters, focus_topic: undefined })}>切回全部问题</Button>}
        <Button variant="ghost" size="sm" aria-label="打开对话记录" onClick={() => setHistoryOpen(true)}><History className="size-3.5" />历史</Button>
        <Button variant="ghost" size="sm" aria-label="新建对话" onClick={newThread}><SquarePen className="size-3.5" />新建</Button>
      </div>

      <div ref={scroll} className="min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]" onScroll={() => {
        const panel = scroll.current
        if (panel) { const next = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 100; nearBottom.current = next; setAtBottom(next) }
      }} aria-label="聊天消息">
        {!current ? <LoadingState label="正在打开对话…" className="mx-auto h-full max-w-3xl px-6 md:px-10" /> : current.messages.length === 0 ? <div key={current.id} className="qiyu-page-enter mx-auto flex min-h-full w-full max-w-3xl flex-col justify-center px-6 py-8 md:px-10">
          <h3 className="text-xl font-semibold tracking-tight">{topicTitle || '想了解哪些客户咨询？'}</h3>
          <p className="mt-3 text-sm leading-7 text-muted-foreground">{topicTitle ? chatScopeLabel(filters) : '查询高频问题、接待回复与知识依据。'}</p>
          <div className="mt-5 max-w-xl">{presets.slice(0, 3).map(({ prompt }) => <button key={prompt} onClick={() => { fillPreset(prompt); input.current?.focus() }} className="voc-chat-suggestion block w-full rounded-md py-2.5 text-left text-sm text-muted-foreground outline-none hover:text-foreground focus-visible:text-foreground focus-visible:ring-2 focus-visible:ring-ring">{prompt}</button>)}</div>
        </div> : <div key={current.id} className="qiyu-page-enter mx-auto w-full max-w-3xl space-y-8 px-5 py-8 md:px-9" role="log" aria-label="对话内容" aria-live="polite">
          {current.messages.map(message => <article key={message.id} className={`voc-chat-message ${message.role === 'user' ? 'flex justify-end' : 'space-y-3'}`}>
            {message.role === 'user' ? <p className="max-w-[88%] whitespace-pre-wrap break-words rounded-lg bg-muted/70 px-4 py-3 text-sm leading-7">{message.content}</p> : <>
              <div className="whitespace-pre-wrap break-words text-sm leading-7 [overflow-wrap:anywhere]">{message.content}</div>
              {!!message.citations?.length && <details className="voc-chat-disclosure text-xs text-muted-foreground"><summary className="w-fit cursor-pointer list-none py-1 underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-ring">查看依据（{message.citations.length}）</summary><div className="space-y-1 pt-2">{message.citations.map((citation, index) => <button key={`${citation.kind}-${citation.id}`} onClick={() => openSource(citation, message)} className={`flex w-full items-start gap-2 py-1.5 text-left text-xs leading-5 underline-offset-4 transition-colors duration-150 hover:text-foreground hover:underline ${selection?.message.id === message.id && selection.citation.id === citation.id ? 'text-foreground underline' : 'text-muted-foreground'}`}><span className="shrink-0 tabular-nums">{index + 1}.</span><span className="min-w-0 flex-1 break-words">{citation.title || `来源 ${citation.id}`}</span></button>)}</div></details>}
              <div className="flex items-start justify-between gap-3"><details className="voc-chat-disclosure min-w-0 text-[11px] leading-5 text-muted-foreground"><summary className="w-fit cursor-pointer list-none py-1 underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-ring">查询范围与模型</summary><div className="space-y-1 pt-1"><p>范围：{message.scope || chatScopeLabel(citationFilters(current.filters, message.queryFilters))}</p>{message.model && <p>模型：{message.model}</p>}</div></details><Button size="icon-sm" variant="ghost" aria-label="复制回答" onClick={() => void copy(message)}>{copied === message.id ? <Check className="size-3.5" /> : <Copy className="size-3.5 text-muted-foreground" />}</Button></div>
            </>}
          </article>)}
          {pending && <LoadingState label="正在查询会话与知识依据…" rows={2} className="min-h-28 py-2" />}
        </div>}
      </div>
      {!atBottom && current?.messages.length ? <Button variant="outline" size="icon-sm" className="absolute bottom-40 right-6 rounded-full bg-background shadow-sm" aria-label="回到最新消息" onClick={() => { if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; nearBottom.current = true; setAtBottom(true) }}><ArrowDown /></Button> : null}

      <div className="shrink-0 px-4 pb-4 pt-2 md:px-8 md:pb-5"><div className="mx-auto max-w-3xl">
        {state.storageWarning && <p role="status" className="mb-2 text-xs text-muted-foreground">本窗口暂时无法保存聊天历史，刷新后可能无法恢复当前对话。</p>}
        {current?.error && <p role="alert" className="mb-2 text-sm text-destructive">{current.error}</p>}
        <form onSubmit={event => { event.preventDefault(); send() }} className="voc-chat-composer rounded-lg border border-input bg-background/75 p-3">
          <textarea ref={input} rows={2} aria-label="向语海Voc提问" aria-describedby={completion ? completionId : undefined} placeholder="输入问题，或用「高频」「排查」补全预设话术…" maxLength={draftLimit} value={current?.draft || ''} disabled={!current || pending} onChange={event => {
            if (!current) return
            setDismissedCompletion(null)
            store.setDraft(current.id, event.target.value)
          }} onKeyDown={event => {
            if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
            if (event.key === 'Escape' && completion && current) {
              event.preventDefault()
              setDismissedCompletion({ threadId: current.id, draft: current.draft })
              return
            }
            if (event.key !== 'Enter' || event.shiftKey) return
            event.preventDefault()
            if (event.repeat) return
            if (completion) acceptCompletion()
            else send()
          }} className="block min-h-14 max-h-40 w-full resize-none bg-transparent px-1 py-1 text-sm leading-6 outline-none placeholder:text-muted-foreground/70 disabled:opacity-60" />
          <div id={completionId} aria-live="polite">
            {completion && <button type="button" onClick={acceptCompletion} className="mt-1 flex w-full items-center justify-between gap-3 rounded-md bg-muted/55 px-2 py-1.5 text-left text-xs leading-5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring">
              <span>{completion}</span><span className="shrink-0 text-[10px]">Enter 补全</span>
            </button>}
          </div>
          <div className="mt-2 flex items-center gap-2 sm:gap-3">
            <span className="min-w-0 max-w-[40%] truncate px-1 py-1 text-[11px] text-muted-foreground" title={gateway.data?.model ? `当前网关模型：${gateway.data.model}` : undefined}>{gateway.data?.model || (gateway.loading ? '正在读取模型' : '使用已配置模型')}</span>
            <span className="ml-auto hidden whitespace-nowrap text-[10px] text-muted-foreground/70 sm:inline">{completion ? 'Esc 取消补全' : 'Enter 发送'} · Shift+Enter 换行</span>
            {pending ? <Button type="button" size="icon-sm" className="ml-auto shrink-0 sm:ml-0" aria-label="停止等待" title="停止等待" onClick={() => current && store.stop(current.id)}><Square className="size-3.5 fill-current" /></Button> : <Button type="submit" size="icon-sm" className="ml-auto shrink-0 rounded-lg sm:ml-0" aria-label="发送问题" disabled={!current?.draft.trim()}><ArrowUp className="size-4" /></Button>}
          </div>
        </form>

      </div></div>
    <Sheet open={historyOpen} onOpenChange={setHistoryOpen}><SheetContent side="left" className="w-72 gap-0 p-0 data-[state=open]:duration-250 data-[state=closed]:duration-200" onCloseAutoFocus={event => { if (deleting) event.preventDefault() }}><SheetHeader className="border-b p-4"><SheetTitle>对话记录</SheetTitle><SheetDescription>本窗口保留的最近对话</SheetDescription></SheetHeader>{history}</SheetContent></Sheet>
    <Sheet open={!!selection} onOpenChange={open => { if (!open) setSelection(null) }}><SheetContent className="w-full overflow-y-auto sm:max-w-3xl data-[state=open]:duration-250 data-[state=closed]:duration-200"><SheetHeader><SheetTitle>回答依据</SheetTitle><SheetDescription>查看来源案例及对应会话记录。</SheetDescription></SheetHeader><div className="px-4 pb-6">{selection && <VocSourceInspector citation={selection.citation} filters={selection.filters} />}</div></SheetContent></Sheet>
    <AlertDialog open={!!deleting} onOpenChange={open => { if (!open) setDeleting(null) }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>删除本窗口对话？</AlertDialogTitle><AlertDialogDescription>将删除本窗口保存的“{deleting?.title}”。</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction onClick={() => { if (deleting) { const next = store.remove(deleting.id); onScopeChange(next.filters) } setDeleting(null) }}>删除对话</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </section>
}

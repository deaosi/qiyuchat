import * as React from 'react'
import { AlertCircle, Archive, ArrowUpRight, CircleCheck, CircleDashed, Layers2, MessagesSquare, RefreshCw, ShieldCheck } from 'lucide-react'
import { LoadingState } from './loading-state'
import type { QAPair } from '@/lib/demo-data'
import { ApiError, apiGet, apiPost, normalizePair, type DetailMessageImage, type DetailRepliesResponse } from '@/lib/api'
import { cn, customerLabel } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'

export interface QADetailProps { pair: QAPair | null; onClose: () => void; source?: 'local'; embedded?: boolean; anchorMessageId?: string; onCloseAutoFocus?: (event: Event) => void }

type Availability = 'available' | 'partial' | 'archived' | 'empty'
type RawContextMessage = { role?: unknown; content?: unknown; time?: unknown; source_message_id?: unknown; ts?: unknown; is_question?: unknown; is_answer?: unknown; images?: DetailMessageImage[] }
interface ContextMessage {
  key: string
  role: string
  content: string
  time: string
  sourceMessageId: string
  ts?: number
  isQuestion: boolean
  images: DetailMessageImage[]
  official?: true
}
interface DetailResponse {
  pair: Record<string, unknown>
  messages?: RawContextMessage[]
  archived?: boolean
  message_count?: number
  context_scope?: string
  availability?: Availability
  can_lookup_replies?: boolean
}
interface DetailData { pair: QAPair; normalizedQuestion: string; messages: ContextMessage[]; archived: boolean; availability?: Availability; canLookupReplies: boolean }

const replyLookupText: Record<DetailRepliesResponse['status'] | 'loading', string> = {
  loading: '正在查询七鱼客服回复…',
  found: '已补充七鱼返回的人工客服回复。',
  no_reply: '七鱼未返回该会话的人工客服回复。',
  unmatched: '未能在七鱼记录中匹配到这段会话。',
  unavailable: '暂时无法查询七鱼客服回复。',
  busy: '七鱼客服回复查询繁忙。',
}

function retainedMessage(message: RawContextMessage, index: number): ContextMessage {
  const sourceMessageId = typeof message.source_message_id === 'string' && message.source_message_id.trim() ? message.source_message_id : ''
  return {
    key: `local-${sourceMessageId}-${index}`,
    role: typeof message.role === 'string' ? message.role : '',
    content: typeof message.content === 'string' ? message.content : '',
    time: typeof message.time === 'string' && message.time ? message.time : '时间未知',
    sourceMessageId,
    ts: typeof message.ts === 'number' && Number.isFinite(message.ts) ? message.ts : undefined,
    isQuestion: message.is_question === true,
    images: message.images ?? [],
  }
}

function mergeOfficialReplies(retained: ContextMessage[], replies: DetailRepliesResponse['messages']): ContextMessage[] {
  const official = replies.map((reply): ContextMessage => ({
    key: `official-${reply.source_message_id}`, role: reply.role, content: reply.content, time: reply.time,
    sourceMessageId: reply.source_message_id, ts: reply.ts, isQuestion: false, images: reply.images ?? [], official: true,
  }))
  // Stable sort retains the saved message order for equal timestamps.
  return [...retained, ...official].sort((a, b) => (a.ts ?? 0) - (b.ts ?? 0))
}

function fullTime(value: string) {
  const match = value.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)/)
  return match ? `${match[1]} ${match[2]}` : '时间未知'
}

function messageRole(message: ContextMessage, pair: QAPair) {
  if (message.official) return '人工客服'
  switch (message.role) {
    case 'user': return `客户 · ${customerLabel(pair.customerName, pair.customerId)}`
    case 'staff': return `客服 · ${pair.staffName || '未记录'}`
    case 'bot': return '机器人'
    case 'system': return '系统'
    default: return '消息'
  }
}

function ConversationImage({ url }: DetailMessageImage) {
  const [expanded, setExpanded] = React.useState(false)
  const [failed, setFailed] = React.useState(false)

  if (failed) return <p className="text-xs opacity-75">图片暂不可查看</p>
  return <button type="button" aria-expanded={expanded} aria-label={expanded ? '缩小会话图片' : '放大会话图片'}
    className={`block w-fit overflow-hidden rounded-md focus-visible:outline-2 focus-visible:outline-offset-2 ${expanded ? 'max-w-full cursor-zoom-out' : 'max-w-[min(100%,14rem)] cursor-zoom-in'}`}
    onClick={() => setExpanded(value => !value)}>
    <img src={url} alt="会话图片" loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setFailed(true)}
      className={`block h-auto max-w-full object-contain ${expanded ? 'max-h-[65vh]' : 'max-h-36'}`} />
  </button>
}

const messageLayout = {
  left: { row: 'justify-start', group: 'items-start', bubble: 'rounded-tl-sm border-border/80 bg-background' },
  right: { row: 'justify-end', group: 'items-end', bubble: 'rounded-tr-sm border-foreground/10 bg-linear-to-b from-foreground/10 to-foreground/[0.06]' },
  center: { row: 'justify-center', group: 'items-center', bubble: 'border-transparent bg-foreground/5 text-muted-foreground' },
} as const

function ConversationMessage({ message, pair, anchored }: { message: ContextMessage; pair: QAPair; anchored: boolean }) {
  const side = message.role === 'staff' ? 'right' : message.role === 'user' ? 'left' : 'center'
  const layout = messageLayout[side]

  return (
    <li data-side={side} data-evidence-anchor={anchored ? '' : undefined} className={cn('flex min-w-0 scroll-my-6', layout.row)}>
      <div className={cn('flex w-fit min-w-0 max-w-[88%] flex-col gap-1.5 sm:max-w-[80%]', layout.group)}>
        <div className={cn('flex max-w-full flex-wrap items-baseline gap-x-2 gap-y-0.5 px-1 text-xs leading-5 text-muted-foreground', layout.row)}>
          <span className="min-w-0">{messageRole(message, pair)}</span>
          <time className="tabular-nums">{message.time}</time>
          {message.sourceMessageId && message.isQuestion && <span className="text-[11px] font-medium text-foreground">本次咨询</span>}
        </div>
        <div className={cn('w-fit min-w-0 max-w-full whitespace-pre-wrap rounded-xl border px-3.5 py-2.5 text-sm leading-6 [overflow-wrap:anywhere]', layout.bubble, anchored && 'ring-2 ring-foreground/35 ring-offset-2 ring-offset-surface')}>
          {(!message.images.length || message.content.trim() !== '[图片]') && <p>{message.content}</p>}
          {message.images.length > 0 && <div className="space-y-2 [&:not(:first-child)]:mt-2">
            {message.images.map((image, index) => <ConversationImage key={`${image.url}-${index}`} url={image.url} />)}
          </div>}
        </div>
      </div>
    </li>
  )
}

export function QADetail({ pair: requestedPair, onClose, source = 'local', embedded = false, anchorMessageId, onCloseAutoFocus }: QADetailProps) {
  const [retry, setRetry] = React.useState(0)
  const [request, setRequest] = React.useState<{ key: string; loading: boolean; data: DetailData | null; error: string | null }>({ key: '', loading: false, data: null, error: null })
  const [lookupRequestedFor, setLookupRequestedFor] = React.useState<DetailData | null>(null)
  const [replyRevision, setReplyRevision] = React.useState(0)
  const [replyLookup, setReplyLookup] = React.useState<{ detail: DetailData; revision: number; result: DetailRepliesResponse } | null>(null)
  const sequence = React.useRef(0)
  const transcript = React.useRef<HTMLOListElement>(null)
  const requestedId = requestedPair?.id
  const requestKey = requestedPair && source ? JSON.stringify([source, requestedPair.id]) : ''
  React.useEffect(() => {
    setLookupRequestedFor(null)
    if (!requestKey) {
      setRequest({ key: '', loading: false, data: null, error: null })
      return
    }
    const [dataSource, id] = JSON.parse(requestKey) as ['local', string]
    const controller = new AbortController()
    const current = ++sequence.current
    setRequest({ key: requestKey, loading: true, data: null, error: null })
    apiGet<DetailResponse>('detail', { source: dataSource, id }, controller.signal)
      .then(result => {
        if (controller.signal.aborted || current !== sequence.current) return
        if (!result?.pair) throw new Error('该问答不存在或已不可用。')
        const archived = Boolean(result.archived)
        setRequest({ key: requestKey, loading: false, error: null, data: {
          pair: { ...normalizePair(result.pair), archived },
          normalizedQuestion: typeof result.pair.q_norm === 'string' ? result.pair.q_norm : '',
          messages: Array.isArray(result.messages) ? result.messages.map(retainedMessage) : [],
          archived,
          availability: ['available', 'partial', 'archived', 'empty'].includes(String(result.availability)) ? result.availability : undefined,
          canLookupReplies: result.can_lookup_replies === true,
        } })
      })
      .catch(error => {
        if (controller.signal.aborted || current !== sequence.current) return
        if (error instanceof ApiError && error.status === 401) return
        setRequest({ key: requestKey, loading: false, data: null, error: error instanceof Error ? error.message : '暂时无法读取会话详情。' })
      })
    return () => controller.abort()
  }, [requestKey, retry])

  const loaded = request.key === requestKey ? request.data : null
  const retainedMessages = loaded?.messages ?? []
  const hasRetainedStaff = retainedMessages.some(message => message.role === 'staff')
  const lookupEligible = loaded?.canLookupReplies && !hasRetainedStaff ? loaded : null
  const lookupDetail = embedded || lookupRequestedFor === lookupEligible ? lookupEligible : null
  React.useEffect(() => {
    if (!lookupDetail || !requestedId) return
    const controller = new AbortController()
    const receive = (result: DetailRepliesResponse) => {
      if (!controller.signal.aborted) setReplyLookup({ detail: lookupDetail, revision: replyRevision, result })
    }
    apiPost<DetailRepliesResponse>('detail/replies', { source, id: requestedId }, undefined, undefined, controller.signal)
      .then(receive)
      .catch(() => receive({ status: 'unavailable', messages: [] }))
    return () => controller.abort()
  }, [lookupDetail, requestedId, replyRevision, source])

  const officialResult = lookupDetail && replyLookup?.detail === lookupDetail && replyLookup.revision === replyRevision ? replyLookup.result : null
  const lookupStatus = lookupDetail ? officialResult?.status ?? 'loading' : null
  const loading = Boolean(requestKey) && (request.key !== requestKey || request.loading)
  const error = request.key === requestKey ? request.error : null
  const pair = loaded?.pair ?? requestedPair
  const normalizedQuestion = loaded?.normalizedQuestion.trim()
    ? loaded.normalizedQuestion : (loading ? '正在读取归一化问法…' : error ? '暂时无法读取归一化问法' : '未记录归一化问法')
  const archived = Boolean(loaded?.archived)
  const officialFound = officialResult?.status === 'found'
  const messages = officialFound ? mergeOfficialReplies(retainedMessages, officialResult.messages) : retainedMessages
  const anchoredKey = anchorMessageId ? messages.find(message => message.sourceMessageId === anchorMessageId)?.key : undefined
  React.useEffect(() => {
    if (anchoredKey) transcript.current?.querySelector('[data-evidence-anchor]')?.scrollIntoView?.({ block: 'nearest' })
  }, [anchoredKey, anchorMessageId, requestKey])
  const availability = loaded?.availability || (archived ? 'archived' : undefined)
  const noStaffMessages = embedded && !lookupStatus && retainedMessages.length > 0 && (availability === 'available' || availability === 'partial') && !hasRetainedStaff
  const content = pair && <div className={embedded ? 'min-w-0 [overflow-wrap:anywhere]' : 'min-h-0 min-w-0 flex-1 overflow-y-auto px-6 pb-8 [overflow-wrap:anywhere]'}>
          {!embedded && <div className="flex flex-wrap gap-2 py-5">
            <Badge variant="secondary" className="rounded-md">{pair.id}</Badge>
            <Badge variant="outline" className="gap-1.5 font-normal">
              {pair.answer.trim() || officialFound ? <CircleCheck className="size-3.5" /> : <CircleDashed className="size-3.5" />}
              {pair.answer.trim() ? '有客服回复' : officialFound ? '已查到人工客服回复' : '未发现回复'}
            </Badge>
            <Badge variant="outline" className="gap-1.5 font-normal text-muted-foreground"><ShieldCheck className="size-3.5" />已脱敏</Badge>
          </div>}

          <dl className={`grid min-w-0 grid-cols-2 gap-x-5 gap-y-5 pb-6 text-sm [&>div]:min-w-0 ${embedded ? 'pt-2' : ''}`}>
            <div><dt className="mb-1.5 text-xs text-muted-foreground">咨询客户</dt><dd className="font-medium">{customerLabel(pair.customerName, pair.customerId)}</dd></div>
            <div><dt className="mb-1.5 text-xs text-muted-foreground">{embedded ? '接待客服' : '客户群 / 接待客服'}</dt><dd>{embedded ? pair.staffName : pair.groupName}</dd>{!embedded && <dd className="mt-1 text-xs text-muted-foreground">{pair.staffName}</dd>}</div>
            <div><dt className="mb-1.5 text-xs text-muted-foreground">提问时间</dt><dd className="tabular-nums">{fullTime(pair.askedAt)}</dd></div>
            <div><dt className="mb-1.5 text-xs text-muted-foreground">会话 ID</dt><dd className="break-all text-xs leading-6">{pair.sessionId}</dd></div>
          </dl>
          {!embedded && <><Separator /><div className="mt-5 rounded-lg border bg-muted/30 p-4">
            <div className="mb-2 flex items-center justify-between gap-3 text-xs text-muted-foreground"><span className="inline-flex items-center gap-1.5"><Layers2 className="size-3.5" />归一化问题</span><span>{pair.clusterId ? `问题簇 ${pair.clusterId}` : '未归类'}</span></div>
            <p className="whitespace-pre-wrap text-sm font-medium [overflow-wrap:anywhere]">{normalizedQuestion}</p>
          </div>
          <section className="py-6">
            <h3 className="mb-3 flex items-center gap-2 text-xs font-medium text-muted-foreground"><ArrowUpRight className="size-3.5" />客服回复</h3>
            <p className="whitespace-pre-wrap text-sm leading-7 [overflow-wrap:anywhere]">{pair.answer.trim() || (officialFound ? '查到的人工客服回复见下方会话记录。' : '当前抽取片段中未发现客服回复。这不代表已经确认该问题未解决。')}</p>
          </section>
          </>}
          <Separator />
          <section className="pt-6" aria-label="聊天记录">
            <h3 className="mb-5 flex items-center gap-2 text-sm font-medium"><MessagesSquare className="size-4" />{embedded ? '聊天记录' : '相关会话上下文'}{!loading && !error && loaded && <span className="ml-auto text-xs font-normal tabular-nums text-muted-foreground">{messages.length} 条</span>}</h3>
            {loading ? <LoadingState label="正在读取相关会话上下文…" rows={4} />
              : error ? <div className="rounded-lg border p-4" role="alert"><div className="flex items-center gap-2 text-sm font-medium"><AlertCircle className="size-4" />暂时无法读取会话详情</div><p className="mt-2 text-sm leading-6 text-muted-foreground">{error}</p><Button variant="outline" size="sm" className="mt-3" onClick={() => setRetry(value => value + 1)}><RefreshCw className="size-3.5" />重新加载</Button></div>
                : <>
                  {availability === 'partial' && <p role="status" className="mb-4 text-sm leading-6 text-muted-foreground">当前仅保留部分聊天记录。</p>}
                  {noStaffMessages && <p className="mb-4 text-sm leading-6 text-muted-foreground">这段已保存的会话记录中没有客服消息。</p>}
                  {!embedded && lookupEligible && !lookupDetail && <Button variant="outline" size="sm" className="mb-4" onClick={() => setLookupRequestedFor(lookupEligible)}><MessagesSquare className="size-3.5" />查询七鱼客服回复</Button>}
                  {lookupStatus && <div className="mb-4 flex flex-wrap items-center gap-x-2 text-sm leading-6 text-muted-foreground">
                    <p role="status">{replyLookupText[lookupStatus]}</p>
                    {(lookupStatus === 'busy' || lookupStatus === 'unavailable') && <Button variant="ghost" size="xs" onClick={() => setReplyRevision(value => value + 1)}>重试查询</Button>}
                  </div>}
                  {availability === 'archived' && <div role="status" className="mb-4 flex items-start gap-3 rounded-lg border bg-muted/30 p-4 text-sm leading-6 text-muted-foreground"><Archive className="mt-1 size-4 shrink-0" /><p>原始聊天记录已归档。</p></div>}
                  {anchorMessageId && !anchoredKey && lookupStatus !== 'loading' && <p role="status" className="mb-4 text-sm text-muted-foreground">当前保留的会话记录中无法定位这条证据。</p>}
                  {messages.length ? <ol ref={transcript} className="space-y-4 rounded-xl border border-border/70 bg-surface px-3 py-4 sm:space-y-5 sm:px-5 sm:py-5" aria-label="会话消息">
                    {messages.map(message => <ConversationMessage key={message.key} message={message} pair={pair} anchored={message.key === anchoredKey} />)}
                  </ol>
                    : availability !== 'archived' && <p className="rounded-lg border bg-muted/20 p-4 text-sm leading-6 text-muted-foreground">当前会话没有可展示的聊天记录。</p>}
                </>}
          </section>
          {!embedded && <p className="mt-7 text-xs leading-6 text-muted-foreground">问答与上下文由服务器返回，并按现有规则脱敏。</p>}
        </div>

  if (embedded) return content
  return (
    <Sheet open={Boolean(pair)} onOpenChange={open => { if (!open) onClose() }}>
      <SheetContent onCloseAutoFocus={onCloseAutoFocus} className="w-full gap-0 sm:max-w-[560px] data-[state=open]:duration-250 data-[state=closed]:duration-200">
        <SheetHeader className="shrink-0 border-b p-6 pr-12">
          <SheetTitle className="text-lg">问答与相关会话上下文</SheetTitle>
          <SheetDescription>查看与当前客户相关的会话片段，核对原话、客服回复与咨询背景。</SheetDescription>
        </SheetHeader>
        {content}
      </SheetContent>
    </Sheet>
  )
}

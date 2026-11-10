import * as React from 'react'
import { AlertCircle, Archive, ArrowDownLeft, ArrowUpRight, CircleCheck, CircleDashed, Layers2, Loader2, MessagesSquare, RefreshCw, ShieldCheck } from 'lucide-react'
import type { QAPair } from '@/lib/demo-data'
import { ApiError, apiGet, normalizePair } from '@/lib/api'
import { customerLabel } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'

export interface QADetailProps { pair: QAPair | null; onClose: () => void; source?: 'local' }

interface ContextMessage { role: string; content: string; time: string }
interface DetailResponse {
  pair: Record<string, unknown>
  messages?: { role?: unknown; content?: unknown; time?: unknown }[]
  archived?: boolean
}
interface DetailData { pair: QAPair; normalizedQuestion: string; messages: ContextMessage[]; archived: boolean }

function fullTime(value: string) {
  return /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(value)
    ? `${value.slice(0, 10)} ${value.slice(11, 16)}` : '时间未知'
}

function messageRole(message: ContextMessage, pair: QAPair) {
  return ({ user: customerLabel(pair.customerName, pair.customerId), staff: `客服 · ${pair.staffName || '未记录'}`, bot: '机器人', system: '系统' })[message.role] || '消息'
}

export function QADetail({ pair: requestedPair, onClose, source = 'local' }: QADetailProps) {
  const [retry, setRetry] = React.useState(0)
  const [request, setRequest] = React.useState<{ key: string; loading: boolean; data: DetailData | null; error: string | null }>({ key: '', loading: false, data: null, error: null })
  const sequence = React.useRef(0)
  const requestKey = requestedPair && source ? JSON.stringify([source, requestedPair.id]) : ''
  React.useEffect(() => {
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
          messages: Array.isArray(result.messages) ? result.messages.map(message => ({
            role: typeof message.role === 'string' ? message.role : '',
            content: typeof message.content === 'string' ? message.content : '',
            time: typeof message.time === 'string' && message.time ? message.time : '时间未知',
          })) : [],
          archived,
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
  const loading = Boolean(requestKey) && (request.key !== requestKey || request.loading)
  const error = request.key === requestKey ? request.error : null
  const pair = loaded?.pair ?? requestedPair
  const normalizedQuestion = loaded?.normalizedQuestion.trim()
    ? loaded.normalizedQuestion : (loading ? '正在读取归一化问法…' : error ? '暂时无法读取归一化问法' : '未记录归一化问法')
  const archived = Boolean(loaded?.archived)
  const messages: ContextMessage[] = loaded?.messages || []

  return (
    <Sheet open={Boolean(pair)} onOpenChange={open => { if (!open) onClose() }}>
      <SheetContent className="w-full gap-0 sm:max-w-[560px] data-[state=open]:duration-250 data-[state=closed]:duration-200">
        <SheetHeader className="shrink-0 border-b p-6 pr-12">
          <SheetTitle className="text-lg">问答与相关会话上下文</SheetTitle>
          <SheetDescription>查看与当前客户相关的会话片段，核对原话、客服回复与咨询背景。</SheetDescription>
        </SheetHeader>
        {pair && <div className="min-h-0 min-w-0 flex-1 overflow-y-auto px-6 pb-8 [overflow-wrap:anywhere]">
          <div className="flex flex-wrap gap-2 py-5">
            <Badge variant="secondary" className="rounded-md">{pair.id}</Badge>
            <Badge variant="outline" className="gap-1.5 font-normal">
              {pair.answer.trim() ? <CircleCheck className="size-3.5" /> : <CircleDashed className="size-3.5" />}
              {pair.answer.trim() ? '有客服回复' : '未发现回复'}
            </Badge>
            <Badge variant="outline" className="gap-1.5 font-normal text-muted-foreground"><ShieldCheck className="size-3.5" />已脱敏</Badge>
          </div>

          <dl className="grid min-w-0 grid-cols-2 gap-x-5 gap-y-5 pb-6 text-sm [&>div]:min-w-0">
            <div><dt className="mb-1.5 text-xs text-muted-foreground">咨询客户</dt><dd className="font-medium">{customerLabel(pair.customerName, pair.customerId)}</dd></div>
            <div><dt className="mb-1.5 text-xs text-muted-foreground">客户群 / 接待客服</dt><dd>{pair.groupName}</dd><dd className="mt-1 text-xs text-muted-foreground">{pair.staffName}</dd></div>
            <div><dt className="mb-1.5 text-xs text-muted-foreground">提问时间</dt><dd className="tabular-nums">{fullTime(pair.askedAt)}</dd></div>
            <div><dt className="mb-1.5 text-xs text-muted-foreground">会话 ID</dt><dd className="break-all text-xs leading-6">{pair.sessionId}</dd></div>
          </dl>
          <Separator />

          <section className="py-6">
            <h3 className="mb-3 flex items-center gap-2 text-xs font-medium text-muted-foreground"><ArrowDownLeft className="size-3.5" />客户原问题</h3>
            <p className="min-w-0 whitespace-pre-wrap text-xl font-semibold leading-relaxed tracking-tight [overflow-wrap:anywhere]">{pair.question}</p>
          </section>
          <div className="rounded-lg border bg-muted/30 p-4">
            <div className="mb-2 flex items-center justify-between gap-3 text-xs text-muted-foreground"><span className="inline-flex items-center gap-1.5"><Layers2 className="size-3.5" />归一化问题</span><span>{pair.clusterId ? `问题簇 ${pair.clusterId}` : '未归类'}</span></div>
            <p className="whitespace-pre-wrap text-sm font-medium [overflow-wrap:anywhere]">{normalizedQuestion}</p>
          </div>
          <section className="py-6">
            <h3 className="mb-3 flex items-center gap-2 text-xs font-medium text-muted-foreground"><ArrowUpRight className="size-3.5" />客服回复</h3>
            <p className="whitespace-pre-wrap text-sm leading-7 [overflow-wrap:anywhere]">{pair.answer.trim() ? pair.answer : '当前抽取片段中未发现客服回复。这不代表已经确认该问题未解决。'}</p>
          </section>
          <Separator />
          <section className="pt-6">
            <h3 className="mb-5 flex items-center gap-2 text-sm font-medium"><MessagesSquare className="size-4" />相关会话上下文</h3>
            {loading ? <div className="flex items-center gap-2 rounded-lg border bg-muted/20 p-5 text-sm text-muted-foreground" role="status"><Loader2 className="size-4 animate-spin" />正在读取相关会话上下文…</div>
              : error ? <div className="rounded-lg border p-4" role="alert"><div className="flex items-center gap-2 text-sm font-medium"><AlertCircle className="size-4" />暂时无法读取会话详情</div><p className="mt-2 text-sm leading-6 text-muted-foreground">{error}</p><Button variant="outline" size="sm" className="mt-3" onClick={() => setRetry(value => value + 1)}><RefreshCw className="size-3.5" />重新加载</Button></div>
                : archived ? <div className="flex items-start gap-3 rounded-lg border bg-muted/30 p-4 text-sm leading-6 text-muted-foreground"><Archive className="mt-1 size-4 shrink-0" /><p>原始上下文已归档，问答资产仍保留。可查看上方问题与回复。</p></div>
                  : messages.length ? <div className="space-y-5">{messages.map((message, index) => <div key={`${index}-${message.role}`} className={`min-w-0 max-w-[90%] ${message.role === 'staff' ? 'ml-auto' : ''}`}><div className="mb-2 flex flex-wrap justify-between gap-x-4 gap-y-1 text-xs text-muted-foreground"><span>{messageRole(message, pair)}</span><time>{message.time}</time></div><div className={message.role === 'staff' ? 'min-w-0 whitespace-pre-wrap rounded-2xl [overflow-wrap:anywhere] rounded-tr-sm bg-foreground px-4 py-3 text-sm leading-7 text-background' : 'min-w-0 whitespace-pre-wrap rounded-2xl [overflow-wrap:anywhere] rounded-tl-sm border bg-muted/35 px-4 py-3 text-sm leading-7'}>{message.content}</div></div>)}</div>
                    : <p className="rounded-lg border bg-muted/20 p-4 text-sm leading-6 text-muted-foreground">当前问答没有可展示的上下文消息。</p>}
          </section>
          <p className="mt-7 text-xs leading-6 text-muted-foreground">问答与上下文由服务器返回，并按现有规则脱敏。</p>
        </div>}
      </SheetContent>
    </Sheet>
  )
}

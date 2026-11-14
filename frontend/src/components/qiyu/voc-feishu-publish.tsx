import { useRef, useState } from 'react'
import { Download, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { DialogClose, DialogFooter } from '@/components/ui/dialog'
import { useApi } from '@/hooks/use-api'
import { ApiError, apiPost, httpsLink, type ApiFilters, type FeishuKind, type FeishuPreview, type FeishuPublication } from '@/lib/api'
import { LoadingMark, LoadingState } from './loading-state'

type Scope = Pick<ApiFilters, 'source' | 'days' | 'start' | 'end' | 'group' | 'staff'> & { kind: FeishuKind }
type Action = { fingerprint: string; receipt?: FeishuPublication; error?: string }
const labels = { topics: '高频问题报告', sops: '知识 SOP' } as const
const countText = new Intl.NumberFormat('zh-CN')

export function useFeishuPublication() {
  const lock = useRef(false)
  const [publishing, setPublishing] = useState(false)
  const [actions, setActions] = useState<Record<string, Action>>({})

  async function publish(request: Scope, preview: FeishuPreview, scopeKey: string, retry: () => void) {
    if (lock.current || !preview.publish_configured || !preview.count || !preview.fingerprint) return
    lock.current = true
    setPublishing(true)
    const submitted = { fingerprint: preview.fingerprint }
    setActions(current => ({ ...current, [scopeKey]: submitted }))
    try {
      const receipt = await apiPost<FeishuPublication>('voc2/feishu/publish', request, JSON.stringify({ fingerprint: preview.fingerprint }), { 'Content-Type': 'application/json' })
      setActions(current => ({ ...current, [scopeKey]: { ...submitted, receipt } }))
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return
      if (error instanceof ApiError && error.status === 409) retry()
      const message = error instanceof Error ? error.message : '发布失败，请重试。'
      setActions(current => ({ ...current, [scopeKey]: { ...submitted, error: message } }))
    } finally {
      lock.current = false
      setPublishing(false)
    }
  }

  return { publishing, actions, publish }
}

function downloadXml(content: string, kind: FeishuKind) {
  const url = URL.createObjectURL(new Blob([content], { type: 'application/xml;charset=UTF-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `语海Voc-${labels[kind]}.xml`
  try { link.click() }
  finally { window.setTimeout(URL.revokeObjectURL.bind(URL, url), 1000) }
}

function periodLabel(start: string, end: string) {
  if (!start && !end) return '全部时间'
  if (!start) return `截至 ${end}`
  if (!end) return `${start} 起`
  return start === end ? start : `${start} 至 ${end}`
}

export function VocFeishuPublish({ active, hasWindow, filters, defaultKind, publication }: {
  active: boolean; hasWindow: boolean; filters: ApiFilters; defaultKind: FeishuKind; publication: ReturnType<typeof useFeishuPublication>
}) {
  const [kind, setKind] = useState(defaultKind)
  const { source, days, start, end, group, staff } = filters
  const request: Scope = { source, days, start, end, group, staff, kind }
  const scopeKey = JSON.stringify(request)
  const { data: preview, loading, refreshing, error, retry } = useApi<FeishuPreview>('voc2/feishu/preview', request, 0, active && hasWindow)
  const action = publication.actions[scopeKey]
  const receipt = action?.fingerprint === preview?.fingerprint ? action?.receipt : undefined
  const receiptUrl = httpsLink(receipt?.url)
  const destinationUrl = httpsLink(preview?.destination)
  const content = preview?.count ? preview.content : undefined
  const ready = Boolean(preview?.publish_configured && preview.count > 0 && preview.fingerprint && !loading && !refreshing && !error)

  return <>
    <div className="min-h-0 space-y-4 overflow-y-auto overscroll-contain px-0.5" aria-busy={loading || refreshing}>
      <div className="flex flex-wrap gap-2" role="group" aria-label="发布内容">
        {(Object.keys(labels) as FeishuKind[]).map(value => <Button key={value} size="sm" variant={kind === value ? 'default' : 'outline'} aria-pressed={kind === value} onClick={() => setKind(value)}>{labels[value]}</Button>)}
      </div>

      {!hasWindow && <p className="rounded-lg border bg-muted/25 p-3 text-sm text-muted-foreground">发布需要明确的统计区间。请选择最近一段时间或填写完整的起止日期。</p>}
      {loading && <LoadingState label="正在准备飞书发布预览…" />}
      {error && <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3 text-sm">
        <p>{error}</p><Button size="sm" variant="outline" onClick={retry}><RefreshCw />重试</Button>
      </div>}

      {preview && <>
        <div className="rounded-xl border bg-linear-to-br from-background to-muted/50 p-3.5">
          <h3 className="text-sm font-semibold [overflow-wrap:anywhere]">{preview.title}</h3>
          <dl className="mt-2 space-y-1 text-xs leading-5 text-muted-foreground">
            <div className="flex flex-wrap gap-x-2"><dt>统计区间</dt><dd className="tabular-nums">{periodLabel(preview.start, preview.end)}</dd></div>
            {(group || staff) && <div className="flex flex-wrap gap-x-2"><dt>筛选范围</dt><dd className="min-w-0 [overflow-wrap:anywhere]">{[group || '全部客户群', staff || '全部客服'].join(' · ')}</dd></div>}
            <div className="flex flex-wrap gap-x-2"><dt>发布目标</dt><dd className="min-w-0 [overflow-wrap:anywhere]">{destinationUrl ? <a href={destinationUrl} target="_blank" rel="noreferrer" className="text-foreground underline underline-offset-4">{preview.destination}</a> : preview.destination || (preview.publish_configured ? '已配置的飞书目标' : '尚未配置')}</dd></div>
            <div className="flex flex-wrap gap-x-2"><dt>发布条目</dt><dd className="tabular-nums">{countText.format(preview.count)} 项{preview.total > preview.count ? `，共 ${countText.format(preview.total)} 项符合当前范围` : ''}</dd></div>
          </dl>
        </div>
        {preview.items.length > 0 ? <section className="space-y-2" aria-label="发布条目预览">
          <ul className="divide-y rounded-lg border">
            {preview.items.map((item, index) => <li key={`${item.title}-${index}`} className="px-3 py-2.5 [overflow-wrap:anywhere]">
              <p className="text-sm font-medium">{item.title}</p>
              {item.summary && <p className="mt-0.5 whitespace-pre-line text-xs leading-5 text-muted-foreground">{item.summary}</p>}
            </li>)}
          </ul>
          {preview.count > preview.items.length && <p className="text-xs text-muted-foreground">预览展示前 {countText.format(preview.items.length)} 项。</p>}
        </section> : <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">当前范围没有可发布的{labels[kind]}。</p>}
        {!preview.publish_configured && <p role="status" className="text-sm text-muted-foreground">当前环境未配置飞书发布。</p>}
      </>}

      {action?.error && <p role="alert" className="rounded-lg border p-3 text-sm [overflow-wrap:anywhere]">{action.error}</p>}
      {receipt && receiptUrl && <div role="status" className="flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3 text-sm">
        <p className="min-w-0 [overflow-wrap:anywhere]">{receipt.reused ? '相同内容已发布' : '发布成功'}{receipt.count !== undefined ? ` · ${countText.format(receipt.count)} 项` : ''}</p>
        <Button asChild variant="outline" size="sm"><a href={receiptUrl} target="_blank" rel="noreferrer">在飞书查看成果</a></Button>
      </div>}
    </div>

    <DialogFooter className="border-t pt-4">
      <DialogClose asChild><Button variant="outline">关闭</Button></DialogClose>
      {content && <Button variant="outline" disabled={loading || refreshing || !!error} onClick={() => downloadXml(content, kind)}><Download />下载 XML</Button>}
      <Button disabled={!ready || publication.publishing || Boolean(receipt)} onClick={() => preview && void publication.publish(request, preview, scopeKey, retry)}>
        {publication.publishing && <LoadingMark />}{publication.publishing ? '正在发布…' : '发布到飞书'}
      </Button>
    </DialogFooter>
  </>
}

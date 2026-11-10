import { useEffect, useRef, useState } from 'react'
import { Check, CheckCheck, ChevronDown, CircleCheck, CircleDashed, Clock3, KeyRound, Layers3, LoaderCircle, RefreshCw, Save, ServerCog, ShieldCheck, TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { LoadingState } from './loading-state'
import { useApi } from '@/hooks/use-api'
import { ApiError, apiPost, type ApiFilters, type GatewayConfig, type GatewayModels } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'

interface Draft { baseUrl: string; model: string; apiKey: string; timeout: string; batchSize: string }
const scope: ApiFilters = { source: 'local' }
const empty: Draft = { baseUrl: '', model: '', apiKey: '', timeout: '', batchSize: '' }
const fromConfig = (value: GatewayConfig): Draft => ({ baseUrl: value.base_url, model: value.model, apiKey: '', timeout: String(value.timeout), batchSize: String(value.batch_size) })
function normalizedBase(value: string): string {
  try { return new URL(value.trim()).toString().replace(/\/+$/, '') } catch { return value.trim().replace(/\/+$/, '') }
}

export function GatewaySettings({ onSaved }: { onSaved: () => void }) {
  const config = useApi<GatewayConfig>('gateway/config', scope)
  const [saved, setSaved] = useState<GatewayConfig | null>(null)
  const [draft, setDraft] = useState<Draft>(empty)
  const [busy, setBusy] = useState<'save' | 'models' | null>(null)
  const [actionError, setActionError] = useState<{ action: 'save' | 'models'; message: string } | null>(null)
  const [models, setModels] = useState<GatewayModels | null>(null)
  const [modelSearch, setModelSearch] = useState('')
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => { if (config.data) { setSaved(config.data); setDraft(fromConfig(config.data)); setModels(null); setActionError(null) } }, [config.data])
  const baseChanged = Boolean(saved && normalizedBase(draft.baseUrl) !== normalizedBase(saved.base_url))
  const needsKey = !saved?.key_configured || baseChanged
  const dirty = Boolean(saved && (draft.baseUrl !== saved.base_url || draft.model !== saved.model || draft.apiKey || draft.timeout !== String(saved.timeout) || draft.batchSize !== String(saved.batch_size)))
  const options = (models?.items || []).filter(item => item.id.toLocaleLowerCase().includes(modelSearch.trim().toLocaleLowerCase()))

  function change(values: Partial<Draft>) {
    setDraft(current => ({ ...current, ...values }))
    setActionError(null)
    if ('baseUrl' in values || 'apiKey' in values) { setModels(null); setModelSearch('') }
  }
  function connectionBody(action: 'save' | 'models' = 'save') {
    let url: URL
    try { url = new URL(draft.baseUrl.trim()) } catch { setActionError({ action, message: '请填写完整的 HTTPS API 地址。' }); return null }
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || draft.baseUrl.trim().length > 2048) {
      setActionError({ action, message: 'API 地址须使用 HTTPS，且不包含账号、密码、查询参数或片段。' }); return null
    }
    if (needsKey && !draft.apiKey.trim()) { setActionError({ action, message: baseChanged ? '更换 API 地址时，请填写新地址对应的 API Key。' : '请填写 API Key。' }); return null }
    if (draft.apiKey.length > 4096 || /[\r\n]/.test(draft.apiKey)) { setActionError({ action, message: 'API Key 不能包含换行，且不能超过 4096 个字符。' }); return null }
    return { base_url: draft.baseUrl.trim(), api_key: draft.apiKey.trim() }
  }
  async function save() {
    if (busy) return
    const connection = connectionBody(); if (!connection) return
    const model = draft.model.trim(), timeout = Number(draft.timeout), batchSize = Number(draft.batchSize)
    if (!model || [...model].length > 200 || /[\u0000-\u001f\u007f]/.test(model)) { setActionError({ action: 'save', message: '请填写 1–200 字的模型 ID，且不包含控制字符。' }); return }
    if (!Number.isInteger(timeout) || timeout < 10 || timeout > 600) { setAdvancedOpen(true); setActionError({ action: 'save', message: '请求等待时间须为 10–600 秒的整数。' }); return }
    if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 16) { setAdvancedOpen(true); setActionError({ action: 'save', message: '每批问答数须为 1–16 的整数。' }); return }
    setBusy('save'); setActionError(null)
    try {
      const result = await apiPost<GatewayConfig>('gateway/config', scope, JSON.stringify({ ...connection, model, timeout, batch_size: batchSize }), { 'Content-Type': 'application/json' })
      if (!mounted.current) return
      setSaved(result); setDraft(fromConfig(result)); onSaved()
      toast.success('模型网关已保存，后续整理任务将使用新配置。')
    } catch (error) { if (mounted.current && !(error instanceof ApiError && error.status === 401)) setActionError({ action: 'save', message: error instanceof Error ? error.message : '保存失败，请稍后重试。' }) }
    finally { if (mounted.current) setBusy(null) }
  }
  async function readModels() {
    if (busy) return
    const connection = connectionBody('models'); if (!connection) return
    setBusy('models'); setActionError(null); setModels(null); setModelSearch('')
    try {
      const result = await apiPost<GatewayModels>('gateway/models', scope, JSON.stringify(connection), { 'Content-Type': 'application/json' })
      if (!mounted.current) return
      setModels(result)
      toast.success(result.items.length ? '已读取模型列表，请选择或填写模型 ID。' : '网关已响应，暂未返回可选模型。可手动填写模型 ID。')
    } catch (error) { if (mounted.current && !(error instanceof ApiError && error.status === 401)) setActionError({ action: 'models', message: error instanceof Error ? error.message : '暂时无法读取模型列表。' }) }
    finally { if (mounted.current) setBusy(null) }
  }

  if (config.loading || (!saved && config.data)) return <Card><CardContent><LoadingState label="正在读取模型网关配置…" rows={5} className="min-h-64" /></CardContent></Card>
  if (config.error || !saved) return <Card><CardContent className="flex min-h-64 flex-col items-center justify-center gap-3 p-6 text-center" role="alert"><TriangleAlert className="size-6 text-muted-foreground" /><p className="max-w-lg text-sm text-muted-foreground">{config.error || '暂时无法读取模型网关配置。'}</p><Button size="sm" variant="outline" onClick={config.retry}><RefreshCw />重试</Button></CardContent></Card>

  return <div className="mx-auto w-full max-w-6xl space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-xl font-semibold tracking-tight">模型网关</h2><p className="mt-1.5 text-sm leading-6 text-muted-foreground">为客户问题整理与知识沉淀配置统一的模型服务。</p></div><Badge variant="outline" className="mt-1 gap-1.5 font-normal">{saved.configured ? <CircleCheck className="size-3.5" /> : <CircleDashed className="size-3.5" />}{saved.configured ? '已配置' : '待配置'}</Badge></div>
    <div className="grid min-w-0 gap-5 lg:grid-cols-[minmax(0,1fr)_280px]">
      <Card className="min-w-0"><CardHeader><div className="flex items-center justify-between gap-3"><CardTitle className="flex items-center gap-2 text-base"><ServerCog className="size-4" />连接配置</CardTitle>{dirty && <Badge variant="secondary" className="font-normal">尚未保存</Badge>}</div><CardDescription>填写兼容 OpenAI 接口的 API 地址、模型 ID 和访问密钥。</CardDescription></CardHeader>
        <form onSubmit={event => { event.preventDefault(); void save() }} autoComplete="off" noValidate>
          <CardContent className="space-y-6">
            <div className="space-y-2"><Label htmlFor="gateway-base">API 地址</Label><Input id="gateway-base" type="url" value={draft.baseUrl} maxLength={2048} disabled={Boolean(busy)} onChange={event => change({ baseUrl: event.target.value })} placeholder="https://api.example.com/v1" spellCheck={false} autoCapitalize="none" aria-describedby="gateway-base-help" /><p id="gateway-base-help" className="text-xs leading-5 text-muted-foreground">填写网关提供的基础地址，一般以 /v1 结尾。</p></div>
            <div className="space-y-2"><div className="flex flex-wrap items-center justify-between gap-2"><Label htmlFor="gateway-key">API Key</Label><span className="inline-flex items-center gap-1 text-xs text-muted-foreground"><KeyRound className="size-3" />{saved.key_configured ? '密钥已配置' : '尚未配置密钥'}</span></div><Input id="gateway-key" name="gateway-new-api-key" type="password" value={draft.apiKey} maxLength={4096} disabled={Boolean(busy)} onChange={event => change({ apiKey: event.target.value })} placeholder={needsKey ? '请输入 API Key' : '留空保留已保存的密钥'} autoComplete="new-password" spellCheck={false} autoCapitalize="none" aria-describedby="gateway-key-help" /><p id="gateway-key-help" className="text-xs leading-5 text-muted-foreground">{baseChanged && saved.key_configured ? '已更换 API 地址，请同时填写新地址对应的密钥。' : saved.key_configured ? '已保存的密钥不会回显。更换密钥时填写新值，留空保留。' : '密钥由服务器保存，用于访问模型服务。'}</p></div>
            <div className="rounded-lg border bg-muted/25 p-4"><div className="flex flex-wrap items-center justify-between gap-3"><div className="space-y-1"><p className="text-sm font-medium">检查连接并读取模型</p><p className="text-xs leading-5 text-muted-foreground">读取当前填写地址的模型列表，不提交会话内容，也不会保存配置。</p></div><Button type="button" size="sm" variant="outline" onClick={() => void readModels()} disabled={Boolean(busy)}>{busy === 'models' ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}读取模型列表</Button></div>{models && <div className="mt-4 space-y-3 border-t pt-4"><p className="flex items-center gap-1.5 text-xs text-muted-foreground"><Check className="size-3.5" />{models.items.length ? `已读取 ${models.total} 个模型，可选择或手动填写模型 ID。` : '网关已响应，暂无可选模型，可手动填写模型 ID。'}</p>{models.items.length > 0 && <><Input value={modelSearch} onChange={event => setModelSearch(event.target.value)} disabled={Boolean(busy)} placeholder="筛选返回的模型…" aria-label="筛选可选模型" maxLength={200} /><Select value={options.some(item => item.id === draft.model) ? draft.model : ''} onValueChange={value => change({ model: value })} disabled={Boolean(busy) || !options.length}><SelectTrigger className="w-full" aria-label="选择网关模型"><SelectValue placeholder={options.length ? '从列表选择模型 ID' : '没有匹配的模型'} /></SelectTrigger><SelectContent>{options.slice(0, 200).map(item => <SelectItem key={item.id} value={item.id}>{item.id}</SelectItem>)}</SelectContent></Select>{options.length > 200 && <p className="text-xs text-muted-foreground">当前显示前 200 项，请输入关键词缩小范围。</p>}</>}</div>}</div>
            <div className="space-y-2"><Label htmlFor="gateway-model">模型 ID</Label><Input id="gateway-model" value={draft.model} maxLength={200} disabled={Boolean(busy)} onChange={event => change({ model: event.target.value })} placeholder="填写网关实际支持的模型 ID" spellCheck={false} autoCapitalize="none" /><p className="text-xs leading-5 text-muted-foreground">用于 VOC 高频问题、知识草稿及每小时自动整理。模型列表不可用时，也可手动填写。</p></div>
            <Separator />
            <details className="group" open={advancedOpen} onToggle={event => setAdvancedOpen(event.currentTarget.open)}><summary className="flex cursor-pointer list-none items-center justify-between text-sm font-medium">高级设置<ChevronDown className="size-4 text-muted-foreground transition-transform group-open:rotate-180" /></summary><div className="mt-4 grid gap-5 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="gateway-timeout">请求等待时间（秒）</Label><Input id="gateway-timeout" type="number" min={10} max={600} step={1} value={draft.timeout} disabled={Boolean(busy)} onChange={event => change({ timeout: event.target.value })} /><p className="text-xs leading-5 text-muted-foreground">单次请求等待时间，10–600 秒。</p></div><div className="space-y-2"><Label htmlFor="gateway-batch">每批问答数</Label><Input id="gateway-batch" type="number" min={1} max={16} step={1} value={draft.batchSize} disabled={Boolean(busy)} onChange={event => change({ batchSize: event.target.value })} /><p className="text-xs leading-5 text-muted-foreground">每次整理 1–16 条问答，较小批次通常更稳定。</p></div></div></details>
            {actionError && <div className="space-y-1 rounded-lg border bg-muted/30 p-3 text-sm leading-6" role="alert"><p className="flex items-start gap-2 font-medium"><TriangleAlert className="mt-1 size-4 shrink-0" />{actionError.action === 'models' ? '模型列表读取失败' : '配置尚未保存'}</p><p className="whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">{actionError.message}</p>{actionError.action === 'models' && <p className="text-xs text-muted-foreground">请检查地址和密钥，或直接填写模型 ID 后保存。</p>}</div>}
          </CardContent>
          <CardFooter className="mt-6 flex flex-wrap items-center justify-between gap-3 border-t pt-5"><p className="flex items-center gap-1.5 text-xs text-muted-foreground"><ShieldCheck className="size-3.5" />密钥不会在页面中回显</p><div className="flex gap-2"><Button type="button" variant="ghost" size="sm" disabled={Boolean(busy) || !dirty} onClick={() => { setDraft(fromConfig(saved)); setActionError(null); setModels(null) }}>恢复已保存配置</Button><Button type="submit" size="sm" disabled={Boolean(busy) || !dirty}>{busy === 'save' ? <LoaderCircle className="animate-spin" /> : <Save />}保存配置</Button></div></CardFooter>
        </form>
      </Card>
      <div className="space-y-4"><Card className="gap-4"><CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Layers3 className="size-4" />统一应用范围</CardTitle></CardHeader><CardContent className="space-y-3 text-sm text-muted-foreground">{['VOC 高频问题归并', '客服回复与知识草稿', '每小时自动整理'].map(text => <p key={text} className="flex items-center gap-2"><CheckCheck className="size-3.5 shrink-0" />{text}</p>)}</CardContent></Card><Card className="gap-4"><CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Clock3 className="size-4" />何时生效</CardTitle></CardHeader><CardContent className="space-y-3 text-xs leading-6 text-muted-foreground"><p>保存后，后续手动及自动任务使用新配置。正在运行的整理任务继续完成。</p><p>读取模型列表只检查该地址的响应，实际模型可用性以整理任务结果为准。</p><p>模型生成内容仍进入待审核，知识不会自动发布。</p></CardContent></Card></div>
    </div>
  </div>
}

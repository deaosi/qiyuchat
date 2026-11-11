import { useEffect, useId, useState } from 'react'
import { Check, FileSpreadsheet, LoaderCircle, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { apiPost } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

interface ImportPreview {
  filename: string
  digest: string
  kind: string
  fields: Record<string, string>
  total_rows: number
  valid_rows: number
  duplicate_rows: number
  skipped_rows: number
  errors: { row: number; reason: string }[]
  sessions: number
  estimated_pairs: number
  unknown_time: number
  samples: { question: string; answer: string; customer_name: string; group_name: string }[]
  can_import: boolean
}

interface ImportResult {
  new_sessions: number
  updated_sessions: number
  qa_pairs: number
}

const fieldLabels: Record<string, string> = {
  session_id: '会话 ID', content: '会话正文', question: '问题', answer: '回复',
  customer_id: '客户 ID', customer_name: '客户名称', group: '客户群',
  staff: '客服', time: '提问时间', end: '结束时间',
}
const numberText = (value: number) => value.toLocaleString('zh-CN')
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败，请稍后重试。'

async function sha256(bytes: ArrayBuffer): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new Error('当前页面无法校验文件，请通过 HTTPS 或本机服务地址打开后重试。')
  }
  const hash = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(hash), value => value.toString(16).padStart(2, '0')).join('')
}

export function ImportDialog({ open, onOpenChange, onImported }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onImported: () => void
}) {
  const inputId = useId()
  const [file, setFile] = useState<File | null>(null)
  const [inputKey, setInputKey] = useState(0)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [previewBytes, setPreviewBytes] = useState<ArrayBuffer | null>(null)
  const [digest, setDigest] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<'preview' | 'import' | null>(null)

  useEffect(() => {
    if (!open) return
    setFile(null)
    setPreview(null)
    setPreviewBytes(null)
    setDigest('')
    setError('')
    setInputKey(value => value + 1)
  }, [open])

  const chooseFile = (next: File | null) => {
    setPreview(null)
    setPreviewBytes(null)
    setDigest('')
    setError('')
    setFile(next)
    if (!next) return
    if (!/\.(csv|xlsx)$/i.test(next.name)) {
      setError('请选择 CSV 或 XLSX 文件；旧版 XLS 请另存为 XLSX。')
      setFile(null)
    } else if (!next.size) {
      setError('文件是空的，请选择包含会话或问答的表格。')
      setFile(null)
    } else if (next.size > 8 * 1024 * 1024) {
      setError('文件超过 8 MB，请拆分后导入。')
      setFile(null)
    }
  }

  const previewFile = async () => {
    if (!file || busy) return
    setBusy('preview')
    setError('')
    setDigest('')
    setPreview(null)
    setPreviewBytes(null)
    try {
      const bytes = await file.arrayBuffer()
      const fileDigest = await sha256(bytes)
      const result = await apiPost<ImportPreview>(
        'import/preview', { source: 'local' }, bytes,
        { 'X-Filename': encodeURIComponent(file.name) },
      )
      if (result.digest !== fileDigest) {
        throw new Error('文件校验不一致，请重新选择文件并预览。')
      }
      setPreview(result)
      if (result.can_import) {
        setPreviewBytes(bytes)
        setDigest(fileDigest)
      }
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(null)
    }
  }

  const confirmImport = async () => {
    if (!file || !preview?.can_import || !previewBytes || !digest || busy) return
    setBusy('import')
    setError('')
    try {
      // Upload exactly the bytes already previewed and approved, never a re-read file.
      const result = await apiPost<ImportResult>(
        'import', { source: 'local' }, previewBytes,
        { 'X-Filename': encodeURIComponent(file.name), 'X-File-Digest': digest },
      )
      setDigest('')
      setPreviewBytes(null)
      toast.success(`导入完成：新增 ${numberText(result.new_sessions)}、更新 ${numberText(result.updated_sessions)} 个会话，全库 ${numberText(result.qa_pairs)} 条问答。`)
      onOpenChange(false)
      onImported()
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(null)
    }
  }

  const currentStep = busy === 'import' ? 3 : busy === 'preview' || preview ? 2 : 1

  return <Dialog open={open} onOpenChange={next => { if (!busy) onOpenChange(next) }}>
    <DialogContent
      className="max-h-[90dvh] overflow-y-auto sm:max-w-3xl"
      showCloseButton={!busy}
      onEscapeKeyDown={event => { if (busy) event.preventDefault() }}
      onInteractOutside={event => { if (busy) event.preventDefault() }}
    >
      <DialogHeader>
        <DialogTitle>导入会话数据</DialogTitle>
        <DialogDescription>文件将上传到当前服务进行解析。预览通过并确认后，才写入本地业务库并执行规则清洗。</DialogDescription>
      </DialogHeader>
      <ol className="grid grid-cols-3 gap-3 rounded-lg border bg-muted/30 p-3 text-sm" aria-label="导入步骤">
        {['选择文件', '预览与检查', '导入并清洗'].map((title, index) => <li key={title} aria-current={currentStep === index + 1 ? 'step' : undefined} className={`flex items-center gap-2 ${currentStep < index + 1 ? 'text-muted-foreground' : 'font-medium'}`}>
          <span className={`flex size-6 shrink-0 items-center justify-center rounded-full border text-xs ${currentStep >= index + 1 ? 'border-primary bg-primary text-primary-foreground' : ''}`}>
            {currentStep > index + 1 ? <Check className="size-3.5" /> : index + 1}
          </span><span>{title}</span>
        </li>)}
      </ol>

      <div className="space-y-3">
        <Label htmlFor={inputId} className="flex items-center gap-2"><FileSpreadsheet className="size-4" />选择 CSV / XLSX 文件</Label>
        <Input key={inputKey} id={inputId} type="file" accept=".csv,.xlsx" disabled={!!busy} onChange={event => chooseFile(event.target.files?.[0] ?? null)} />
        <p className="text-xs text-muted-foreground">单个文件最多 8 MB、5,000 行。支持会话正文或问答表。<a href="/api/template?source=local" className="ml-1 underline underline-offset-4">下载导入模板</a></p>
        {file && <p className="text-xs text-muted-foreground">{file.name} · {(file.size / 1024).toFixed(1)} KB</p>}
      </div>

      {error && <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">{error}</div>}
      {busy && <div role="status" className="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />{busy === 'preview' ? '正在上传并解析文件，尚未写入数据…' : '正在导入并清洗，请保持页面打开…'}</div>}

      {preview && <div className="space-y-4" aria-live="polite">
        <div className="grid grid-cols-3 gap-3">
          {[[preview.valid_rows, '有效行'], [preview.sessions, '会话'], [preview.estimated_pairs, '预计有效问答']].map(([value, label]) => <div key={label} className="rounded-lg border p-3"><div className="text-xl font-semibold tabular-nums">{numberText(Number(value))}</div><div className="mt-1 text-xs text-muted-foreground">{label}</div></div>)}
        </div>
        <div className="flex flex-wrap gap-2"><Badge variant="outline">{preview.kind}</Badge><Badge variant="secondary">共 {numberText(preview.total_rows)} 行</Badge><Badge variant="secondary">跳过错误 {numberText(preview.skipped_rows)} 行</Badge><Badge variant="secondary">重复 {numberText(preview.duplicate_rows)} 行</Badge><Badge variant="secondary">缺少时间 {numberText(preview.unknown_time)} 行</Badge></div>
        <div className="space-y-2"><h3 className="text-sm font-medium">字段识别</h3><div className="overflow-x-auto rounded-lg border"><Table><TableHeader><TableRow><TableHead>原表列名</TableHead><TableHead>导入字段</TableHead></TableRow></TableHeader><TableBody>{Object.entries(preview.fields).map(([key, column]) => <TableRow key={key}><TableCell>{column}</TableCell><TableCell>{fieldLabels[key] || key}</TableCell></TableRow>)}</TableBody></Table></div></div>
        {preview.errors.length > 0 && <div className="space-y-2"><h3 className="text-sm font-medium">需要检查的行</h3><div className="max-h-40 overflow-y-auto rounded-lg border p-3 text-sm">{preview.errors.map((item, index) => <p key={`${item.row}-${index}`} className="py-1">第 {item.row} 行：{item.reason}</p>)}{preview.skipped_rows > preview.errors.length && <p className="pt-2 text-xs text-muted-foreground">仅展示前 {preview.errors.length} 条错误，其余错误行也会跳过。</p>}</div></div>}
        <div className="space-y-2"><h3 className="text-sm font-medium">脱敏样本</h3>{preview.samples.length ? preview.samples.map((sample, index) => <div key={index} className="space-y-2 rounded-lg border bg-muted/20 p-3 text-sm"><p className="whitespace-pre-wrap break-words"><span className="mr-2 font-medium">问</span>{sample.question}</p><p className="whitespace-pre-wrap break-words text-muted-foreground"><span className="mr-2 font-medium text-foreground">答</span>{sample.answer || '未发现客服回复'}</p><p className="text-xs text-muted-foreground">{[sample.customer_name, sample.group_name].filter(Boolean).join(' · ')}</p></div>) : <p className="rounded-lg border p-3 text-sm text-muted-foreground">可以读取会话，但没有抽取到有效问题。导入后会记录对应处理状态。</p>}</div>
        <div className="rounded-lg border bg-muted/30 p-3 text-xs leading-relaxed text-muted-foreground">{preview.can_import ? '确认后只导入有效行。相同会话 ID 按完整快照更新，旧问答尾部可能被移除；请导入完整会话，不要只导入增量片段。缺少会话 ID 时按内容生成稳定标识。' : '没有可以导入的有效会话，请修正文件后重新预览。'}</div>
      </div>}

      <DialogFooter className="gap-2 border-t pt-4">
        <Button variant="outline" disabled={!!busy} onClick={() => onOpenChange(false)}>取消</Button>
        <Button variant="outline" disabled={!file || !!busy} onClick={previewFile}>{busy === 'preview' ? <LoaderCircle className="animate-spin" /> : <FileSpreadsheet />}预览文件</Button>
        <Button disabled={!preview?.can_import || !digest || !previewBytes || !!busy} onClick={confirmImport}>{busy === 'import' ? <LoaderCircle className="animate-spin" /> : <Upload />}确认导入并清洗</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}

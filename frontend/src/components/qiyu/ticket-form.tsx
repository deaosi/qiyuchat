import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { ZoomableImage } from './zoomable-image'

export type TicketAttachment = { url: string; name: string }
export type TicketField = { label: string; value: string; images?: TicketAttachment[]; attachments?: TicketAttachment[] }
type TicketFormData = {
  priority: string
  description: string
  fields?: TicketField[]
  images?: TicketAttachment[]
  attachments?: TicketAttachment[]
}

const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

export function ticketTime(value: number | null): string {
  if (typeof value !== 'number' || value <= 0 || !Number.isFinite(new Date(value).getTime())) return '—'
  return timeFormat.format(value).replaceAll('/', '-')
}

export function TicketAttachments({ images = [], attachments = [] }: { images?: TicketAttachment[]; attachments?: TicketAttachment[] }) {
  return <div className="space-y-3">
    {attachments.length > 0 && <ul className="space-y-2">{attachments.map(file => <li key={file.url}>
      <Button asChild variant="outline" size="sm" className="h-auto max-w-full whitespace-normal py-1.5 text-left [overflow-wrap:anywhere]">
        <a href={file.url} download>{file.name || '下载附件'}</a>
      </Button>
    </li>)}</ul>}
    {images.map(({ url, name }) => {
      const label = name || '工单截图'
      return <ZoomableImage key={url} url={url} title="工单图片预览" alt={label} viewportLabel="工单图片查看区"
        thumbnailAlt={label} triggerLabel={`查看图片：${label}`} imageClassName="rounded-md border bg-background" />
    })}
  </div>
}

function TicketFormRow({ label, children }: { label: string; children: ReactNode }) {
  return <div className="grid min-w-0 gap-1.5 sm:grid-cols-[9rem_minmax(0,1fr)] sm:items-start sm:gap-5">
    <dt className="text-sm font-medium leading-6 sm:pt-1 sm:text-right">{label}</dt>
    <dd className="min-w-0 rounded-md border bg-background px-3 py-1.5 text-sm leading-6 [overflow-wrap:anywhere]">{children}</dd>
  </div>
}

export function TicketForm({ ticket }: { ticket: TicketFormData }) {
  const fields = ticket.fields ?? []
  const priority = fields.find(field => field.label === '优先级')
  const request = fields.find(field => field.label === '问题需求')
  const attachmentField = fields.find(field => /附件|截图/.test(field.label))
  const fieldUrls = new Set(fields.flatMap(field => [...(field.images ?? []), ...(field.attachments ?? [])].map(file => file.url)))
  const images = (ticket.images ?? []).filter(file => !fieldUrls.has(file.url))
  const attachments = (ticket.attachments ?? []).filter(file => !fieldUrls.has(file.url))
  const hasMedia = images.length > 0 || attachments.length > 0
  return <dl aria-label="工单提报" className="space-y-3">
    <TicketFormRow label="优先级">{priority?.value || ticket.priority || '—'}</TicketFormRow>
    {fields.filter(field => field.label !== '优先级').map((field, index) => {
      const fieldImages = field === attachmentField ? [...(field.images ?? []), ...images] : field.images
      const fieldAttachments = field === attachmentField ? [...(field.attachments ?? []), ...attachments] : field.attachments
      return <TicketFormRow label={field.label} key={`${field.label}-${index}`}>
        {field.value && <p className="whitespace-pre-wrap">{field.value}</p>}
        {fieldImages?.length || fieldAttachments?.length ?
          <div className={field.value ? 'mt-2' : ''}><TicketAttachments images={fieldImages} attachments={fieldAttachments} /></div> :
          !field.value && <span className="text-muted-foreground">{field === attachmentField ? '暂无附件。' : '—'}</span>}
      </TicketFormRow>
    })}
    {(!request || (ticket.description && ticket.description !== request.value)) && <TicketFormRow label={request ? '工单描述' : '问题需求'}>
      <p className="min-h-12 whitespace-pre-wrap">{ticket.description || '—'}</p>
    </TicketFormRow>}
    {!attachmentField && (hasMedia || !fields.some(field => field.images?.length || field.attachments?.length)) &&
      <TicketFormRow label="附件／截图">{hasMedia ? <TicketAttachments images={images} attachments={attachments} /> :
        <span className="text-muted-foreground">暂无附件。</span>}</TicketFormRow>}
  </dl>
}

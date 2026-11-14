import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { TicketForm, ticketTime, type TicketAttachment, type TicketField } from './ticket-form'

type GuestTicket = {
  title: string
  status: string
  priority: string
  created_at: number | null
  updated_at: number | null
  description: string
  fields?: Pick<TicketField, 'label' | 'value' | 'images'>[]
  images?: TicketAttachment[]
}

export type GuestTicketPayload = { kind: 'ticket'; ticket: GuestTicket }

const ticketPath = /^\/guest\/ticket\/[A-Za-z0-9_-]{43}$/
const imageId = /^[a-f0-9]{64}$/

export function VocGuestTicket({ ticket }: GuestTicketPayload) {
  const path = window.location.pathname
  const imagePrefix = ticketPath.test(path) ? `${path}/images/` : ''
  function scopedImages(images: TicketAttachment[] | undefined) {
    if (!imagePrefix || !Array.isArray(images)) return []
    return images.filter(image => typeof image?.url === 'string' && image.url.startsWith(imagePrefix)
      && imageId.test(image.url.slice(imagePrefix.length)))
  }
  const fields = ticket.fields?.map(({ label, value, images }) => ({ label, value, images: scopedImages(images) }))
  const form = { priority: ticket.priority, description: ticket.description, fields, images: scopedImages(ticket.images) }
  const dates = [
    { label: '创建时间', value: ticket.created_at },
    { label: '更新时间', value: ticket.updated_at },
  ].filter(({ value }) => typeof value === 'number' && value > 0)

  return <div className="min-h-screen bg-linear-to-b from-muted/50 via-background to-background">
    <header className="border-b bg-background">
      <div className="mx-auto flex min-h-14 max-w-4xl items-center justify-between gap-4 px-4 sm:px-6">
        <div className="flex min-w-0 items-center gap-3 text-sm">
          <span className="shrink-0 font-semibold">语海Voc</span>
          <Separator orientation="vertical" className="h-4 self-center" />
          <span className="truncate text-muted-foreground">七鱼工单</span>
        </div>
        <Badge variant="outline" className="shrink-0 font-normal text-muted-foreground">访客阅览</Badge>
      </div>
    </header>
    <main className="qiyu-page-enter mx-auto w-full max-w-4xl px-4 py-5 sm:p-6 sm:pt-8">
      <article className="overflow-hidden rounded-xl border bg-background shadow-sm" aria-labelledby="ticket-heading">
        <div className="space-y-3 border-b bg-linear-to-b from-muted/55 to-background p-5 sm:px-8 sm:py-6">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <h1 id="ticket-heading" className="min-w-0 text-xl font-semibold tracking-tight [overflow-wrap:anywhere]">{ticket.title}</h1>
            {ticket.status && <Badge variant="secondary" className="font-normal">{ticket.status}</Badge>}
          </div>
          {dates.length > 0 && <dl className="flex flex-wrap gap-x-5 gap-y-1 text-xs leading-5 text-muted-foreground">
            {dates.map(({ label, value }) => <div key={label} className="flex flex-wrap gap-x-2">
              <dt>{label}</dt><dd className="tabular-nums">{ticketTime(value)}</dd>
            </div>)}
          </dl>}
        </div>
        <section aria-labelledby="ticket-form-heading" className="space-y-4 p-5 sm:px-8 sm:py-6">
          <h2 id="ticket-form-heading" className="text-sm font-semibold">工单提报</h2>
          <TicketForm ticket={form} />
        </section>
      </article>
    </main>
  </div>
}

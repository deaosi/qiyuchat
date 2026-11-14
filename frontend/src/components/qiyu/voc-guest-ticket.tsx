import { useEffect, useRef } from 'react'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { ConversationMessageBody } from './conversation-image'
import { ConversationBubble, ConversationTranscript, guestChatRoles } from './conversation-transcript'
import { TicketAttachments, TicketForm, ticketTime, type TicketAttachment, type TicketField } from './ticket-form'
import { TicketHistory, TicketInfo, TicketTechnicalFeedback, type TechnicalFeedback, type TicketEvent } from './ticket-details-view'

type GuestTicket = {
  id?: string
  title: string
  status: string
  priority: string
  submitter?: string
  category?: string
  template?: string
  created_at: number | null
  updated_at: number | null
  description: string
  fields?: TicketField[]
  images?: TicketAttachment[]
  attachments?: TicketAttachment[]
}

type GuestSession = {
  started_at: number | null
  customer_name: string
  messages: { text: string; time: string; role: string; sender_name?: string; images?: string[] }[]
}

type GuestFollowup = {
  time: number | null
  images: TicketAttachment[]
  attachments: TicketAttachment[]
}

export type GuestTicketPayload = {
  kind: 'ticket'
  ticket: GuestTicket
  history?: TicketEvent[]
  history_available?: boolean
  sessions?: GuestSession[]
  technical_feedback?: Omit<TechnicalFeedback, 'error' | 'feishu_url'> | null
  followup_attachments?: GuestFollowup[]
}

const ticketPath = /^\/guest\/ticket\/[A-Za-z0-9_-]{43}$/
const imageId = /^[a-f0-9]{64}$/
const followupAnchor = 'ticket-followup-attachments'

export function VocGuestTicket({ ticket, history, history_available, sessions, technical_feedback, followup_attachments }: GuestTicketPayload) {
  const path = window.location.pathname
  const followupHeading = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    if (window.location.hash === `#${followupAnchor}`) {
      followupHeading.current?.focus()
    }
  }, [])
  function isScoped(url: unknown, type: 'images' | 'attachments') {
    const prefix = `${path}/${type}/`
    return ticketPath.test(path) && typeof url === 'string' && url.startsWith(prefix)
      && imageId.test(url.slice(prefix.length))
  }
  function scopedMedia(items: TicketAttachment[] | undefined, type: 'images' | 'attachments') {
    return items?.filter(item => isScoped(item?.url, type)) ?? []
  }
  function scopedFields(fields: TicketField[] | undefined) {
    return fields?.map(({ label, value, images, attachments }) => ({
      label, value, images: scopedMedia(images, 'images'), attachments: scopedMedia(attachments, 'attachments'),
    }))
  }
  const form = { ...ticket, fields: scopedFields(ticket.fields),
    images: scopedMedia(ticket.images, 'images'), attachments: scopedMedia(ticket.attachments, 'attachments') }
  const events = history?.map(event => ({
    ...event, details: scopedFields(event.details) ?? [],
    images: scopedMedia(event.images, 'images'), attachments: scopedMedia(event.attachments, 'attachments'),
  })) ?? []
  const followups = followup_attachments?.map(({ time, images, attachments }) => ({
    time, images: scopedMedia(images, 'images'), attachments: scopedMedia(attachments, 'attachments'),
  })).filter(item => item.images.length || item.attachments.length)

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
          {ticket.id && <p className="text-xs text-muted-foreground">七鱼工单 #{ticket.id}</p>}
        </div>
        <section aria-labelledby="ticket-form-heading" className="space-y-4 p-5 sm:px-8 sm:py-6">
          <h2 id="ticket-form-heading" className="text-sm font-semibold">工单提报</h2>
          <TicketForm ticket={form} />
        </section>
        <div className="space-y-6 px-5 pb-6 sm:px-8">
          <TicketInfo ticket={ticket} />
          {technical_feedback && <TicketTechnicalFeedback feedback={{
            status: technical_feedback.status, reply: technical_feedback.reply,
            resolved_version: technical_feedback.resolved_version, received_at: technical_feedback.received_at,
            writeback_status: technical_feedback.writeback_status, written_at: technical_feedback.written_at,
          }} />}
          {followups && <section className="space-y-2" aria-labelledby={followupAnchor}>
            <h2 id={followupAnchor} ref={followupHeading} tabIndex={-1} className="scroll-mt-4 text-sm font-semibold">跟进附件</h2>
            {followups.length ? <ul className="divide-y rounded-lg border">
              {followups.map((item, index) => <li className="space-y-2 p-4" key={`${item.time}-${index}`}>
                <p className="text-xs tabular-nums text-muted-foreground">{ticketTime(item.time)}</p>
                <TicketAttachments images={item.images} attachments={item.attachments} />
              </li>)}
            </ul> : <p className="text-sm text-muted-foreground">{history_available ? '暂无可查看的跟进附件。' : '暂时无法读取跟进附件。'}</p>}
          </section>}
          <section aria-label="处理记录" className="space-y-2">
            <h2 className="text-sm font-semibold">处理记录</h2>
            <TicketHistory history={events} available={history_available === true} />
          </section>
          {!!sessions?.length && <section className="space-y-2" aria-labelledby="ticket-session-heading">
            <h2 id="ticket-session-heading" className="text-sm font-semibold">关联会话</h2>
            {sessions.map((session, index) => <details key={index} className="rounded-lg border">
              <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
                {session.customer_name || '会话'} <time className="ml-2 text-xs font-normal tabular-nums text-muted-foreground">{ticketTime(session.started_at)}</time>
              </summary>
              <div className="px-3 pb-3">
                <ConversationTranscript>
                  {session.messages.map((message, messageIndex) => {
                    const role = Object.hasOwn(guestChatRoles, message.role)
                      ? guestChatRoles[message.role as keyof typeof guestChatRoles] : { side: 'center', label: '消息' } as const
                    return <ConversationBubble key={messageIndex} side={role.side} sender={message.sender_name || role.label} time={message.time}>
                      <ConversationMessageBody text={message.text} imageUrls={message.images?.filter(url => isScoped(url, 'images')) ?? []} />
                    </ConversationBubble>
                  })}
                </ConversationTranscript>
              </div>
            </details>)}
          </section>}
        </div>
      </article>
    </main>
  </div>
}

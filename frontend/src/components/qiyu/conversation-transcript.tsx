import type { ComponentProps, ReactNode } from 'react'
import { MessagesSquare } from 'lucide-react'
import { cn } from '@/lib/utils'

const messageLayout = {
  left: { row: 'justify-start', group: 'items-start', bubble: 'rounded-tl-sm border-border/80 bg-background' },
  right: { row: 'justify-end', group: 'items-end', bubble: 'rounded-tr-sm border-foreground/10 bg-linear-to-b from-foreground/10 to-foreground/[0.06]' },
  center: { row: 'justify-center', group: 'items-center', bubble: 'border-transparent bg-foreground/5 text-muted-foreground' },
} as const

export function ConversationBubble({ side, sender, senderNote, time, question, anchored, children }: {
  side: keyof typeof messageLayout
  sender: string
  senderNote?: string
  time?: string
  question?: boolean
  anchored?: boolean
  children: ReactNode
}) {
  const layout = messageLayout[side]
  return <li data-side={side} data-evidence-anchor={anchored ? '' : undefined} className={cn('flex min-w-0 scroll-my-6', layout.row)}>
    <div className={cn('flex w-fit min-w-0 max-w-[88%] flex-col gap-1.5 sm:max-w-[80%]', layout.group)}>
      <div className={cn('flex max-w-full flex-wrap items-baseline gap-x-2 gap-y-0.5 px-1 text-xs leading-5 text-muted-foreground', layout.row)}>
        <span className={cn('min-w-0', senderNote && 'font-medium text-foreground')}>{sender}</span>
        {senderNote && <span className="text-[11px]">{senderNote}</span>}
        {time && <time className="tabular-nums">{time}</time>}
        {question && <span className="text-[11px] font-medium text-foreground">本次咨询</span>}
      </div>
      <div className={cn('w-fit min-w-0 max-w-full whitespace-pre-wrap rounded-xl border px-3.5 py-2.5 text-sm leading-6 [overflow-wrap:anywhere]', layout.bubble, anchored && 'ring-2 ring-foreground/35 ring-offset-2 ring-offset-surface')}>
        {children}
      </div>
    </div>
  </li>
}

export function ConversationTranscript({ children, ...props }: Pick<ComponentProps<'ol'>, 'ref' | 'children'>) {
  return <ol {...props} className="space-y-4 rounded-xl border border-border/70 bg-surface px-3 py-4 sm:space-y-5 sm:px-5 sm:py-5" aria-label="会话消息">
    {children}
  </ol>
}

export function ConversationSection({ children, title = '聊天记录', count }: {
  children: ReactNode
  title?: string
  count?: number
}) {
  return <section className="pt-6" aria-label="聊天记录">
    <h3 className="mb-5 flex items-center gap-2 text-sm font-medium">
      <MessagesSquare className="size-4" />{title}
      {count !== undefined && <span className="ml-auto text-xs font-normal tabular-nums text-muted-foreground">{count} 条</span>}
    </h3>
    {children}
  </section>
}

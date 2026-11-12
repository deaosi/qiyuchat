import type { ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'

type DetailMetric = { label: string; value: ReactNode }

export function TopicSummary({ title, badge, description, metrics, metadata, action }: {
  title: string
  badge?: string
  description?: string
  metrics: DetailMetric[]
  metadata?: ReactNode
  action?: ReactNode
}) {
  return <div className="space-y-4 rounded-xl border bg-linear-to-br from-background to-muted/55 p-4 sm:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-lg font-semibold tracking-tight [overflow-wrap:anywhere]">{title}</h3>
          {badge && <Badge variant="outline">{badge}</Badge>}
        </div>
        {description && <p className="text-sm text-muted-foreground [overflow-wrap:anywhere]">{description}</p>}
      </div>
      {action}
    </div>
    {metrics.length > 0 && <dl className="grid grid-cols-3 gap-3 border-t pt-3 text-sm tabular-nums">
      {metrics.map(metric => <div key={metric.label} className="min-w-0">
        <dt className="text-xs text-muted-foreground">{metric.label}</dt>
        <dd className="mt-1 font-semibold [overflow-wrap:anywhere]">{metric.value}</dd>
      </div>)}
    </dl>}
    {metadata && <p className="text-xs leading-5 text-muted-foreground">{metadata}</p>}
  </div>
}

export function DecisionCaseCard({ diagnosis, metadata, action, question, intro, facts, children }: {
  diagnosis?: string
  metadata?: string
  action?: ReactNode
  question?: string
  intro?: ReactNode
  facts: { label: string; content: ReactNode }[]
  children?: ReactNode
}) {
  return <article className="space-y-4 rounded-xl border p-4 sm:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 space-y-1.5">
        {diagnosis && <Badge variant="secondary" className="font-normal">{diagnosis}</Badge>}
        {metadata && <p className="text-xs text-muted-foreground">{metadata}</p>}
      </div>
      {action && <div className="flex flex-wrap gap-2">{action}</div>}
    </div>
    {question && <p className="whitespace-pre-wrap text-sm font-medium leading-6 [overflow-wrap:anywhere]">{question}</p>}
    {intro}
    {facts.length > 0 && <dl className="grid gap-2 md:grid-cols-3">
      {facts.map(({ label, content }) => <div key={label} className="min-w-0 rounded-lg border bg-muted/25 p-3">
        <dt className="mb-2 text-xs font-medium">{label}</dt><dd>{content}</dd>
      </div>)}
    </dl>}
    {children}
  </article>
}

export function DecisionCaseSection({ title, children }: { title: string; children: ReactNode }) {
  return <section className="space-y-2 border-t pt-3"><h5 className="text-sm font-medium">{title}</h5>{children}</section>
}

export function EvidenceCaseCard({ label, time, question, children, footer }: {
  label?: ReactNode
  time?: string
  question: string
  children?: ReactNode
  footer: ReactNode
}) {
  return <article className="qiyu-page-enter space-y-3 rounded-lg border bg-muted/15 p-4">
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-xs text-muted-foreground">
      <span>{label}</span>{time && <time>{time}</time>}
    </div>
    <p className="whitespace-pre-wrap break-words text-sm font-medium leading-6">{question}</p>
    {children}
    <div className="flex flex-wrap items-center justify-between gap-2">{footer}</div>
  </article>
}

export function SopDetailHeader({ title, status, verified, kind, metadata }: {
  title: ReactNode
  status?: string
  verified?: boolean
  kind?: ReactNode
  metadata?: ReactNode
}) {
  return <div data-slot="sheet-header" className="flex shrink-0 flex-col gap-2 border-b bg-linear-to-b from-muted/40 to-background px-5 py-5 pr-10 sm:px-7 sm:pr-12">
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      {status && <Badge variant={verified ? 'default' : 'secondary'} className="font-normal">{status}</Badge>}
      {kind && <span>{kind}</span>}
    </div>
    {title}
    {metadata && <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">{metadata}</div>}
  </div>
}

const sopSectionLayout = {
  normal: 'space-y-2',
  conditions: 'space-y-2 border-b pb-5',
  flow: 'space-y-3',
  result: 'space-y-2 border-t pt-5',
  gaps: 'space-y-1 rounded-lg bg-muted/45 px-4 py-3',
} as const

export function SopDetailSection({ title, layout = 'normal', children }: {
  title: string
  layout?: keyof typeof sopSectionLayout
  children: ReactNode
}) {
  return <section className={sopSectionLayout[layout]}>
    <h3 className={layout === 'gaps' ? 'text-sm font-medium' : 'text-sm font-semibold'}>{title}</h3>
    {children}
  </section>
}

export function SopTimeline({ children }: { children: ReactNode }) {
  return <ol className="ml-2.5 space-y-4 border-l pl-4">{children}</ol>
}

export function SopTimelineStep({ index, phase, outcome, feedback, children }: {
  index: number
  phase?: string
  outcome?: string
  feedback?: ReactNode
  children: ReactNode
}) {
  return <li className="relative min-w-0 space-y-1">
    <span className="absolute -left-[1.7rem] top-0 grid size-5 place-items-center rounded-full border bg-background text-[10px] tabular-nums text-muted-foreground" aria-hidden="true">{index + 1}</span>
    {phase && <div className="flex flex-wrap items-center gap-2 text-xs"><span className="font-medium">{phase}</span><span className="text-muted-foreground">{outcome}</span></div>}
    {children}
    {feedback && <div className="mt-2 space-y-1 rounded-md bg-muted/40 px-3 py-2 text-muted-foreground"><p className="text-xs">客户反馈</p>{feedback}</div>}
  </li>
}

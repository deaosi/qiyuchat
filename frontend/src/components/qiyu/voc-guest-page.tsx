import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ConversationBubble, ConversationSection, ConversationTranscript } from './conversation-transcript'
import { DecisionCaseCard, DecisionCaseSection, EvidenceCaseCard, SopDetailHeader, SopDetailSection, SopTimeline, SopTimelineStep, TopicSummary } from './voc-detail-display'

type GuestSection = { title: string; lines: string[] }
type GuestMessage = { role: string; time: string; text: string; anchor?: boolean }
type GuestCase = {
  title: string
  category: string
  time: string
  sections: GuestSection[]
  messages: GuestMessage[]
  scope: 'context' | 'limited' | 'pair'
}
type GuestEntry = {
  kind: 'topics' | 'sops'
  title: string
  summary: string
  detail: string
  start: string
  end: string
  taxonomy?: { primary: string; secondary: string; count?: number }[]
  metrics?: { label: string; value: string | number }[]
  sections?: GuestSection[]
}
export type GuestPayload = { entry: GuestEntry; cases: GuestCase[]; total_cases: number }
type GuestDetailProps = GuestPayload & { period: string; onOpen: (index: number, locate?: boolean) => void }

const facts = new Set(['电商后台设置', '知汇鸽场景条件', '实际触发结果'])
const stepTitles = new Set(['处理流程', '处理步骤'])
const sopLayouts = new Map<string, 'conditions' | 'result' | 'gaps'>([
  ['适用条件', 'conditions'],
  ['复测结果', 'result'],
  ['待补证据', 'gaps'],
] as const)
const count = new Intl.NumberFormat('zh-CN').format

function sectionsOf(entry: GuestEntry): GuestSection[] {
  if (entry.sections?.length) return entry.sections
  const sections: GuestSection[] = [{ title: '成果详情', lines: [] }]
  for (const text of entry.detail?.split('\n') ?? []) {
    const line = text.trim()
    if (!line) continue
    if (entry.kind === 'sops' && (stepTitles.has(line) || sopLayouts.has(line) || line === '问题现象' || line === '人工回复记录')) {
      sections.push({ title: line, lines: [] })
    } else {
      sections[sections.length - 1].lines.push(line)
    }
  }
  return sections.filter(section => section.lines.length)
}

function GuestQuotes({ lines, compact = false }: { lines: string[]; compact?: boolean }) {
  return <div className="space-y-1.5">
    {lines.map((line, index) => <p key={index} className={compact
      ? 'whitespace-pre-wrap text-xs leading-5 text-muted-foreground [overflow-wrap:anywhere]'
      : 'whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]'}>{line}</p>)}
  </div>
}

function Taxonomy({ rows }: { rows: NonNullable<GuestEntry['taxonomy']> }) {
  const hasCount = rows.some(row => row.count !== undefined)
  return <section className="space-y-3" aria-labelledby="guest-taxonomy">
    <h2 className="font-medium" id="guest-taxonomy">问题分类</h2>
    <div className="overflow-hidden rounded-xl border">
      <Table aria-labelledby="guest-taxonomy" className="table-fixed">
        <TableHeader className="bg-muted/55">
          <TableRow>
            <TableHead scope="col" className="w-[36%] px-3 text-xs text-muted-foreground sm:px-4">一级分类</TableHead>
            <TableHead scope="col" className="px-3 text-xs text-muted-foreground sm:px-4">二级分类</TableHead>
            {hasCount && <TableHead scope="col" className="w-20 px-3 text-right text-xs text-muted-foreground sm:w-24 sm:px-4">咨询次数</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row, index) => <TableRow key={index}>
            <TableCell className="px-3 py-3 font-medium whitespace-normal [overflow-wrap:anywhere] sm:px-4">{row.primary || '—'}</TableCell>
            <TableCell className="px-3 py-3 whitespace-normal [overflow-wrap:anywhere] sm:px-4">{row.secondary || '—'}</TableCell>
            {hasCount && <TableCell className="px-3 py-3 text-right tabular-nums text-muted-foreground sm:px-4">{row.count === undefined ? '—' : count(row.count)}</TableCell>}
          </TableRow>)}
        </TableBody>
      </Table>
    </div>
  </section>
}

function GuestDecisionCase({ item, question, open }: { item: GuestCase; question?: string; open?: () => void }) {
  return <DecisionCaseCard diagnosis={item.category} metadata={item.time}
    question={question}
    action={open && <Button variant="outline" size="sm" onClick={open}>查看会话上下文<ArrowUpRight /></Button>}
    facts={item.sections.filter(section => facts.has(section.title)).map(section => ({
      label: section.title, content: <GuestQuotes lines={section.lines} compact={section.lines.every(line => line.startsWith('待补查'))} />,
    }))}>
    {item.sections.filter(section => !facts.has(section.title)).map(section =>
      <DecisionCaseSection key={section.title} title={section.title}><GuestQuotes lines={section.lines} /></DecisionCaseSection>)}
  </DecisionCaseCard>
}

function GuestCases({ cases, total, first, onOpen }: {
  cases: GuestCase[]
  total: number
  first: number
  onOpen: (index: number) => void
}) {
  if (first && cases.length <= first) return null
  return <section className="space-y-3" aria-labelledby="guest-cases">
    <div className="flex items-center justify-between gap-3">
      <h2 id="guest-cases" className="font-medium">{first ? '其他会话依据' : '示例案例'}</h2>
      {cases.length > 0 && <span className="text-xs tabular-nums text-muted-foreground">
        {total > cases.length ? `可查看 ${count(cases.length)} / ${count(total)} 条` : `${count(cases.length - first)} 条`}
      </span>}
    </div>
    {cases.length ? cases.slice(first).map((item, index) => {
      const caseIndex = index + first
      return <EvidenceCaseCard key={caseIndex} label={item.category} time={item.time} question={item.title || `会话 ${caseIndex + 1}`}
        footer={<Button className="ml-auto" variant="outline" size="sm" onClick={() => onOpen(caseIndex)}>查看详情<ArrowUpRight /></Button>} />
    }) : <p className="rounded-lg border bg-muted/20 p-4 text-sm text-muted-foreground">当前条目暂无可公开查看的会话上下文。</p>}
  </section>
}

function GuestTopic({ entry, cases, total_cases: total, period, onOpen }: GuestDetailProps) {
  const representative = cases[0]
  return <div className="space-y-6 p-4 sm:p-6">
    <TopicSummary title={entry.title} titleAs="h1" description={entry.metrics?.length ? undefined : entry.summary}
      metrics={(entry.metrics ?? []).map(metric => ({
        label: metric.label,
        value: typeof metric.value === 'number' || /^\d+$/.test(metric.value)
          ? `${count(Number(metric.value))}${metric.label === '咨询次数' ? ' 次' : metric.label === '独立会话' ? ' 个' : metric.label === '可识别商家' ? ' 家' : ''}`
          : metric.value,
      }))} metadata={period} />
    {!!entry.taxonomy?.length && <Taxonomy rows={entry.taxonomy} />}
    {sectionsOf(entry).map(section => <SopDetailSection key={section.title} title={section.title} layout={sopLayouts.get(section.title)}>
      <GuestQuotes lines={section.lines} compact={section.title === '待补证据'} />
    </SopDetailSection>)}
    {representative && <section className="space-y-3" aria-label="代表案例">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-medium">处理判断 · 代表案例</h2>
        {total > cases.length && cases.length === 1 && <span className="text-xs tabular-nums text-muted-foreground">可查看 1 / {count(total)} 条</span>}
      </div>
      <GuestDecisionCase item={representative} question={representative.title || '会话 1'} open={() => onOpen(0)} />
    </section>}
    <GuestCases cases={cases} total={total} first={representative ? 1 : 0} onOpen={onOpen} />
  </div>
}

function GuestSopSteps({ lines }: { lines: string[] }) {
  const steps: { text: string; phase?: string; outcome?: string; feedback?: string }[] = []
  for (const line of lines) {
    const previous = steps.at(-1)
    if (line.startsWith('客户反馈：') && previous) {
      previous.feedback = line.slice('客户反馈：'.length)
      continue
    }
    const text = line.replace(/^\s*\d+\s*(?:[.．)]\s+|、\s*)/, '')
    const match = /^(排查|处理|复测|升级处理) · ([^\n]+)\n([\s\S]+)$/.exec(text)
    steps.push(match ? { phase: match[1], outcome: match[2], text: match[3] } : { text })
  }
  return <SopTimeline>
    {steps.map((step, index) => <SopTimelineStep key={index} index={index} phase={step.phase} outcome={step.outcome}
      feedback={step.feedback && <GuestQuotes lines={[step.feedback]} />}>
      <GuestQuotes lines={[step.text]} />
    </SopTimelineStep>)}
  </SopTimeline>
}

function GuestSop({ entry, cases, total_cases: total, period, onOpen }: GuestDetailProps) {
  const [kind, status] = entry.summary.split(' · ', 2)
  return <>
    <SopDetailHeader title={<h1 className="text-lg leading-7 font-semibold [overflow-wrap:anywhere]">{entry.title}</h1>}
      kind={kind} status={status} verified={status === '效果已验证'} metadata={<>
        {period && <span className="tabular-nums">{period}</span>}
        {cases[0]?.messages.some(message => message.anchor) && <Button variant="ghost" size="xs" onClick={() => onOpen(0, true)}>定位问题原话<ArrowUpRight /></Button>}
      </>} />
    <div className="space-y-6 px-5 py-6 sm:px-7">
      {!!entry.taxonomy?.length && <Taxonomy rows={entry.taxonomy} />}
      {sectionsOf(entry).filter(section => section.title !== '问题现象' || section.lines.some(line => line !== entry.title)).map(section => {
        const isFlow = stepTitles.has(section.title)
        return <SopDetailSection key={section.title} title={isFlow ? '处理流程' : section.title} layout={isFlow ? 'flow' : sopLayouts.get(section.title)}>
          {isFlow ? <GuestSopSteps lines={section.lines} />
            : section.title === '人工回复记录' ? <div className="divide-y">
              {section.lines.map((line, index) => <div key={index} className="py-2"><GuestQuotes lines={[line]} /></div>)}
            </div>
            : <GuestQuotes lines={section.lines} compact={section.title === '待补证据'} />}
        </SopDetailSection>
      })}
      <GuestCases cases={cases} total={total} first={0} onOpen={onOpen} />
    </div>
  </>
}

const chatRoles = {
  user: { side: 'left', label: '客户' },
  customer: { side: 'left', label: '客户' },
  staff: { side: 'right', label: '客服' },
  bot: { side: 'center', label: '机器人' },
  system: { side: 'center', label: '系统' },
} as const

function GuestTranscript({ item, locate }: { item: GuestCase; locate: boolean }) {
  const transcript = useRef<HTMLOListElement>(null)
  useEffect(() => {
    if (!locate) return
    const frame = requestAnimationFrame(() => {
      transcript.current?.querySelector('[data-evidence-anchor]')?.scrollIntoView?.({ block: 'nearest' })
    })
    return () => cancelAnimationFrame(frame)
  }, [item, locate])

  const note = item.scope === 'pair' ? '以下为该条目关联的脱敏问答。'
    : item.scope === 'limited' ? '会话较长，当前展示与本次咨询相关的片段。'
      : '已脱敏 · 北京时间'
  return <ConversationSection count={item.messages.length}>
    {item.messages.length ? <>
      <p className="mb-4 text-sm leading-6 text-muted-foreground">{note}</p>
      <ConversationTranscript ref={transcript}>
        {item.messages.map((message, index) => {
          const role = Object.hasOwn(chatRoles, message.role)
            ? chatRoles[message.role as keyof typeof chatRoles] : { side: 'center', label: '消息' } as const
          return <ConversationBubble key={index} side={role.side} sender={role.label} time={message.time} question={message.anchor} anchored={message.anchor}>
            <p>{message.text}</p>
          </ConversationBubble>
        })}
      </ConversationTranscript>
    </> : <p className="rounded-lg border bg-muted/20 p-4 text-sm leading-6 text-muted-foreground">当前条目暂无可公开查看的会话上下文。</p>}
  </ConversationSection>
}

export function VocGuestPage({ entry, cases, total_cases }: GuestPayload) {
  const [selection, setSelection] = useState<{ index: number; open: boolean; locate: boolean } | null>(null)
  const trigger = useRef<HTMLElement | null>(null)
  const item = selection && cases[selection.index]
  const caseTime = item?.time || item?.messages.find(message => message.anchor)?.time
  const isSop = entry.kind === 'sops'
  const typeLabel = isSop ? '处理 SOP' : 'VOC高频问题'
  const period = entry.start && entry.end
    ? `统计区间 ${entry.start === entry.end ? entry.start : `${entry.start} 至 ${entry.end}`}` : ''
  const open = (index: number, locate = false) => {
    trigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setSelection({ index, open: true, locate })
  }
  const detailProps = { entry, cases, total_cases, period, onOpen: open }
  return <div className="min-h-screen bg-linear-to-b from-muted/50 via-background to-background">
    <header className="border-b bg-background">
      <div className="mx-auto flex min-h-14 max-w-5xl items-center justify-between gap-4 px-4 sm:px-6">
        <div className="flex min-w-0 items-center gap-3 text-sm">
          <span className="shrink-0 font-semibold">语海Voc</span>
          <Separator orientation="vertical" className="h-4 self-center" />
          <span className="truncate text-muted-foreground">{typeLabel}</span>
        </div>
        <Badge variant="outline" className="shrink-0 font-normal text-muted-foreground">访客阅览</Badge>
      </div>
    </header>
    <main className="qiyu-page-enter mx-auto w-full max-w-5xl px-4 pt-5 pb-10 sm:px-6 sm:pt-8 sm:pb-14">
      <Card className="overflow-hidden py-0">
        {isSop ? <GuestSop {...detailProps} /> : <GuestTopic {...detailProps} />}
      </Card>
    </main>
    <Sheet open={selection?.open === true} onOpenChange={next => setSelection(current => current && { ...current, open: next })}>
      <SheetContent className="w-full gap-0 sm:max-w-3xl data-[state=open]:duration-250 data-[state=closed]:duration-200"
        onCloseAutoFocus={event => { event.preventDefault(); trigger.current?.focus() }}>
        <SheetHeader className="shrink-0 border-b px-4 pr-12 sm:px-6">
          <SheetTitle>会话上下文</SheetTitle>
          <SheetDescription>查看本次咨询的脱敏会话记录。</SheetDescription>
        </SheetHeader>
        {item && selection && <div className="qiyu-page-enter min-h-0 flex-1 overflow-y-auto overscroll-contain p-4 sm:p-6">
          {(item.category || caseTime) && <div className="flex flex-wrap items-center gap-2 border-b pb-4 text-xs text-muted-foreground">
              {item.category && <Badge variant="secondary" className="font-normal">{item.category}</Badge>}
              {caseTime && <time className="tabular-nums">{caseTime}</time>}
          </div>}
          <GuestTranscript item={item} locate={selection.locate} />
          {(isSop || selection.index > 0) && item.sections.length > 0 && <section className="mt-6 space-y-3" aria-label="处理判断">
            <h3 className="text-sm font-medium">处理判断</h3>
            <GuestDecisionCase item={item} />
          </section>}
        </div>}
      </SheetContent>
    </Sheet>
  </div>
}

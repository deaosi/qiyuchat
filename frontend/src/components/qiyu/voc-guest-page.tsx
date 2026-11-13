import { useEffect, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ConversationMessageBody } from './conversation-image'
import { ConversationBubble, ConversationSection, ConversationSheet, ConversationTranscript } from './conversation-transcript'
import { DecisionCaseCard, DecisionCaseSection, EvidenceCaseCard, SopDetailHeader, SopDetailSection, SopTimeline, SopTimelineStep } from './voc-detail-display'
import type { TopicDetail } from './voc-next-workspace'
import { TopicDetailPanel, TopicWorkbench } from './voc-topic-detail'
import type { OpenTopicEvidence } from './voc-topic-evidence'

type GuestSection = { title: string; lines: string[] }
type GuestMessage = { role: string; time: string; text: string; sender_name?: string; sender_label?: string; anchor?: boolean; id?: string; images?: string[] }
type GuestCase = {
  title: string
  category: string
  time: string
  customer_name?: string
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
  sections?: GuestSection[]
}
export type GuestPayload = {
  entry: GuestEntry
  cases: GuestCase[]
  total_cases: number
  topic_detail?: TopicDetail
  topic_variants?: { topic_detail: TopicDetail; case_indices: number[] }[]
}
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

function GuestDecisionCase({ item }: { item: GuestCase }) {
  return <DecisionCaseCard diagnosis={item.category} metadata={item.time}
    facts={item.sections.filter(section => facts.has(section.title)).map(section => ({
      label: section.title, content: <GuestQuotes lines={section.lines} compact={section.lines.every(line => line.startsWith('待补查'))} />,
    }))}>
    {item.sections.filter(section => !facts.has(section.title)).map(section =>
      <DecisionCaseSection key={section.title} title={section.title}><GuestQuotes lines={section.lines} /></DecisionCaseSection>)}
  </DecisionCaseCard>
}

function GuestCases({ cases, total, onOpen }: {
  cases: GuestCase[]
  total: number
  onOpen: (index: number) => void
}) {
  return <section className="space-y-3" aria-labelledby="guest-cases">
    <div className="flex items-center justify-between gap-3">
      <h2 id="guest-cases" className="font-medium">示例案例</h2>
      {cases.length > 0 && <span className="text-xs tabular-nums text-muted-foreground">
        {total > cases.length ? `可查看 ${count(cases.length)} / ${count(total)} 条` : `${count(cases.length)} 条`}
      </span>}
    </div>
    {cases.length ? cases.map((item, index) =>
      <EvidenceCaseCard key={index} label={[item.customer_name?.trim(), item.category].filter(Boolean).join(' · ')} time={item.time} question={item.title || `会话 ${index + 1}`}
        footer={<Button type="button" className="ml-auto" variant="outline" size="xs" aria-label="跳转到会话上下文" onClick={() => onOpen(index)}>跳转</Button>} />
    ) : <p className="rounded-lg border bg-muted/20 p-4 text-sm text-muted-foreground">当前条目暂无可公开查看的会话上下文。</p>}
  </section>
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
        {cases[0]?.messages.some(message => message.anchor) && <Button type="button" variant="outline" size="xs" aria-label="跳转到问题原话" onClick={() => onOpen(0, true)}>跳转</Button>}
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
      <GuestCases cases={cases} total={total} onOpen={onOpen} />
    </div>
  </>
}

const chatRoles = {
  user: { side: 'left', label: '客户' },
  customer: { side: 'left', label: '客户' },
  staff: { side: 'right', label: '客服' },
  group_participant: { side: 'center', label: '群成员' },
  combined_transcript: { side: 'center', label: '合并记录' },
  unidentified: { side: 'center', label: '身份未确认' },
  bot: { side: 'center', label: '机器人' },
  system: { side: 'center', label: '系统' },
} as const
const guestEvidencePath = /^\/guest\/evidence\/[A-Za-z0-9_-]{43}$/
const guestImageId = /^(?:[0-9]|1[0-9])-[a-f0-9]{64}$/
const anonymousGroupLabel = /^群成员 [1-9][0-9]*$/

function GuestTranscript({ item, locate, title }: { item: GuestCase; locate: boolean | string; title?: string }) {
  const transcript = useRef<HTMLOListElement>(null)
  useEffect(() => {
    if (!locate) return
    const frame = requestAnimationFrame(() => {
      transcript.current?.querySelector('[data-evidence-anchor]')?.scrollIntoView?.({ block: 'nearest' })
    })
    return () => cancelAnimationFrame(frame)
  }, [item, locate])

  const hasCombinedTranscript = item.messages.some(message => message.role === 'combined_transcript')
  const note = hasCombinedTranscript ? '历史记录只保留了合并文本，无法区分每条消息的发送者。'
    : item.scope === 'pair' ? '以下为该条目关联的脱敏问答。'
    : item.scope === 'limited' ? '会话较长，当前展示与本次咨询相关的片段。'
      : '已脱敏 · 北京时间'
  const guestPath = window.location.pathname
  const imageBase = guestEvidencePath.test(guestPath) ? `${guestPath}/images/` : ''
  return <ConversationSection title={title} count={item.messages.length}>
    {item.messages.length ? <>
      <p className="mb-4 text-sm leading-6 text-muted-foreground">{note}</p>
      <ConversationTranscript ref={transcript}>
        {item.messages.map((message, index) => {
          const role = Object.hasOwn(chatRoles, message.role)
            ? chatRoles[message.role as keyof typeof chatRoles] : { side: 'center', label: '消息' } as const
          const anonymousSender = typeof message.sender_label === 'string' && anonymousGroupLabel.test(message.sender_label) ? message.sender_label : ''
          const senderName = typeof message.sender_name === 'string' ? message.sender_name.trim() : ''
          const sender = message.role === 'combined_transcript' ? role.label : senderName || anonymousSender || role.label
          const anchored = typeof locate === 'string' ? message.id === locate : message.anchor
          const imageUrls = imageBase && Array.isArray(message.images)
            ? message.images.filter(id => typeof id === 'string' && guestImageId.test(id)).map(id => imageBase + id) : []
          return <ConversationBubble key={index} side={role.side} sender={sender} time={message.time} question={message.anchor && (role.side === 'left' || message.role === 'group_participant')} anchored={anchored}>
            <ConversationMessageBody text={message.text} imageUrls={imageUrls} />
          </ConversationBubble>
        })}
      </ConversationTranscript>
    </> : <p className="rounded-lg border bg-muted/20 p-4 text-sm leading-6 text-muted-foreground">当前条目暂无可公开查看的会话上下文。</p>}
  </ConversationSection>
}

export function VocGuestPage({ entry, cases, total_cases, topic_detail, topic_variants }: GuestPayload) {
  const [selection, setSelection] = useState<{ index: number; open: boolean; locate: boolean | string } | null>(null)
  const [variantIndex, setVariantIndex] = useState(0)
  const trigger = useRef<HTMLElement | null>(null)
  const item = selection && cases[selection.index]
  const caseTime = item?.time || item?.messages.find(message => message.anchor)?.time
  const caseCustomer = item?.customer_name?.trim()
  const isSop = entry.kind === 'sops'
  const activeVariant = topic_variants?.[variantIndex]
  const topicDetail = activeVariant?.topic_detail ?? topic_detail
  const period = entry.start && entry.end
    ? `统计区间 ${entry.start === entry.end ? entry.start : `${entry.start} 至 ${entry.end}`}` : ''
  const open = (index: number, locate: boolean | string = false) => {
    trigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setSelection({ index, open: true, locate })
  }
  const openTopicEvidence: OpenTopicEvidence = (evidence, messageId) => {
    const match = /^guest-case-(\d+)$/.exec(evidence.id)
    if (!match) return
    const index = Number(match[1])
    if (cases[index] && (!activeVariant || activeVariant.case_indices.includes(index))) open(index, messageId)
  }
  const sheetProps = {
    open: selection?.open === true,
    onOpenChange: (next: boolean) => setSelection(current => current && { ...current, open: next }),
    onCloseAutoFocus: (event: Event) => { event.preventDefault(); trigger.current?.focus() },
  }
  const conversation = item && selection && <>
    {(item.category || caseCustomer || caseTime) && <div className="flex flex-wrap items-center gap-2 border-b pb-4 text-xs text-muted-foreground">
      {caseCustomer && <span className="min-w-0 font-medium text-foreground [overflow-wrap:anywhere]"><span className="sr-only">咨询客户 </span>{caseCustomer}</span>}
      {item.category && <Badge variant="secondary" className="font-normal">{item.category}</Badge>}
      {caseTime && <time className="tabular-nums">{caseTime}</time>}
    </div>}
    <GuestTranscript item={item} locate={selection.locate} title={isSop ? undefined : '相关会话上下文'} />
  </>

  if (!isSop) return <div className="min-h-screen bg-muted/30">
    <main className="mx-auto flex min-h-screen w-full flex-col bg-background sm:max-w-3xl xl:max-w-4xl">
      <TopicDetailPanel standalone exchangeOnly={Boolean(activeVariant)}>
        {topic_variants && topic_variants.length > 1 && <div className="mb-4 space-y-2" role="group" aria-label="选择具体问法">
          <p className="text-sm font-medium">选择具体问法</p>
          <div className="grid max-h-56 gap-2 overflow-y-auto pr-1">
            {topic_variants.map(({ topic_detail: { topic } }, index) => <Button key={topic.id} type="button"
              variant={index === variantIndex ? 'default' : 'outline'} size="sm" aria-pressed={index === variantIndex}
              className="h-auto min-w-0 justify-start whitespace-normal py-2 text-left [overflow-wrap:anywhere]"
              onClick={() => { setVariantIndex(index); setSelection(null) }}>
              {topic.question} · {count(topic.consultation_count ?? 0)} 次咨询
            </Button>)}
          </div>
        </div>}
        {topicDetail ? <TopicWorkbench key={variantIndex} detail={topicDetail} onOpenEvidence={openTopicEvidence} readOnly exchangeOnly={Boolean(activeVariant)} />
          : <p className="py-8 text-center text-sm text-muted-foreground">当前链接暂无可展示的主题详情。</p>}
      </TopicDetailPanel>
    </main>
    <ConversationSheet {...sheetProps}>
      {conversation && <div className="min-h-0 min-w-0 flex-1 overflow-y-auto px-6 pt-6 pb-8 [overflow-wrap:anywhere]">{conversation}</div>}
    </ConversationSheet>
  </div>

  return <div className="min-h-screen bg-linear-to-b from-muted/50 via-background to-background">
    <header className="border-b bg-background">
      <div className="mx-auto flex min-h-14 max-w-5xl items-center justify-between gap-4 px-4 sm:px-6">
        <div className="flex min-w-0 items-center gap-3 text-sm">
          <span className="shrink-0 font-semibold">语海Voc</span>
          <Separator orientation="vertical" className="h-4 self-center" />
          <span className="truncate text-muted-foreground">处理 SOP</span>
        </div>
        <Badge variant="outline" className="shrink-0 font-normal text-muted-foreground">访客阅览</Badge>
      </div>
    </header>
    <main className="qiyu-page-enter mx-auto w-full max-w-5xl px-4 pt-5 pb-10 sm:px-6 sm:pt-8 sm:pb-14">
      <Card className="overflow-hidden py-0">
        <GuestSop entry={entry} cases={cases} total_cases={total_cases} period={period} onOpen={open} />
      </Card>
    </main>
    <Sheet open={sheetProps.open} onOpenChange={sheetProps.onOpenChange}>
      <SheetContent className="w-full gap-0 sm:max-w-3xl data-[state=open]:duration-250 data-[state=closed]:duration-200"
        onCloseAutoFocus={sheetProps.onCloseAutoFocus}>
        <SheetHeader className="shrink-0 border-b px-4 pr-12 sm:px-6">
          <SheetTitle>会话上下文</SheetTitle>
          <SheetDescription>查看本次咨询的脱敏会话记录。</SheetDescription>
        </SheetHeader>
        {item && <div className="qiyu-page-enter min-h-0 flex-1 overflow-y-auto overscroll-contain p-4 sm:p-6">
          {conversation}
          {item.sections.length > 0 && <section className="mt-6 space-y-3" aria-label="处理判断">
            <h3 className="text-sm font-medium">处理判断</h3>
            <GuestDecisionCase item={item} />
          </section>}
        </div>}
      </SheetContent>
    </Sheet>
  </div>
}

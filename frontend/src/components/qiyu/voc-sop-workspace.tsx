import { Fragment, useCallback, useMemo, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, Copy, Search } from 'lucide-react'
import { toast } from 'sonner'
import { useBackgroundRefresh } from '@/hooks/use-background-refresh'
import { normalizePair, type ApiFilters, type VocPilotEvidence, type VocPlaybook, type VocPlaybookBranch, type VocKnowledgeLibraryData, type VocLibraryKnowledge } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { QueryPanel, type OpenEvidencePair } from './voc-next-workspace'
import { useVocFirstPageList } from './voc-pilot-workspaces'
import { displayTime } from './voc-topic-evidence'
import { SopDetailHeader, SopDetailSection, SopTimeline, SopTimelineStep } from './voc-detail-display'

type Props = { filters: ApiFilters; refreshKey?: number; onOpenPair: OpenEvidencePair }
type CompleteBranch = VocPlaybookBranch & Required<Pick<VocPlaybookBranch, 'condition_evidence' | 'retest_evidence' | 'result_evidence'>>
type CompleteItem = { item: VocPlaybook; branches: CompleteBranch[] }
type FullList = ReturnType<typeof useVocFirstPageList<VocKnowledgeLibraryData>>['fullList']

const pageSize = 8
const count = new Intl.NumberFormat('zh-CN').format
const rowColumns = 'grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 lg:grid-cols-[minmax(0,1fr)_6rem_10rem_6rem]'

function hasEvidence(sources?: VocPilotEvidence[]): sources is VocPilotEvidence[] {
  return Boolean(sources?.length && sources.every(source => source.qa_id > 0 && source.ts > 0 && source.message_id.trim() && source.quote.trim()))
}

function completeBranch(branch: VocPlaybookBranch): branch is CompleteBranch {
  return branch.status === 'verified' && Boolean(branch.condition.trim() && branch.retest.trim())
    && hasEvidence(branch.condition_evidence) && hasEvidence(branch.retest_evidence) && hasEvidence(branch.result_evidence)
    && branch.steps.length > 0 && branch.steps.every(step => step.text.trim() && hasEvidence(step.evidence))
}

function openSource(source: VocPilotEvidence, onOpenPair: OpenEvidencePair) {
  onOpenPair(normalizePair({ qa_id: source.qa_id, question: source.quote, asked_at: source.ts }), source.message_id)
}

function EvidenceLinks({ sources, onOpenPair }: { sources: VocPilotEvidence[]; onOpenPair: OpenEvidencePair }) {
  const unique = [...new Map(sources.map(source => [`${source.qa_id}:${source.message_id}`, source])).values()]
  if (!unique.length) return null
  return <div className="flex flex-wrap gap-2">
    {unique.map((source, index) => {
      const order = unique.length > 1 ? ` ${index + 1}` : ''
      return <Button key={`${source.qa_id}:${source.message_id}`} type="button" variant="outline" size="xs"
        onClick={() => openSource(source, onOpenPair)} aria-label={`跳转到问答 ${source.qa_id} 的原话${order}`}>
        跳转{order}
      </Button>
    })}
  </div>
}

function sameRetestAndResult(branch: CompleteBranch) {
  return branch.result_evidence.some(source => branch.retest === source.quote || branch.retest === `客户实测原话：${source.quote}`)
}

async function copySop(title: string, branch: CompleteBranch) {
  const text = [title, `适用条件 ${branch.condition}`, '处理步骤',
    ...branch.steps.map((step, index) => `${index + 1}. ${step.text}`),
    sameRetestAndResult(branch) ? '' : `复测方法 ${branch.retest}`,
    ...branch.result_evidence.map(source => `复测结果 ${source.quote}`),
    branch.escalation.trim() && hasEvidence(branch.escalation_evidence) ? `升级处理 ${branch.escalation}` : ''].filter(Boolean).join('\n')
  try {
    await navigator.clipboard.writeText(text)
    toast.success('已复制 SOP。')
  } catch {
    toast.error('剪贴板暂不可用，可选中文字复制。')
  }
}

function SopBranch({ title, branch, onOpenPair }: { title: string; branch: CompleteBranch; onOpenPair: OpenEvidencePair }) {
  const sameRetest = sameRetestAndResult(branch)
  return <article className="space-y-5 rounded-lg border px-4 py-5 sm:px-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 space-y-2">
        <SopDetailSection title="适用条件">
          <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{branch.condition}</p>
          <EvidenceLinks sources={branch.condition_evidence} onOpenPair={onOpenPair} />
        </SopDetailSection>
      </div>
      <Button type="button" variant="outline" size="sm" onClick={() => void copySop(title, branch)}><Copy />复制 SOP</Button>
    </div>
    <SopDetailSection title="处理步骤" layout="flow">
      <SopTimeline>
        {branch.steps.map((step, index) => <SopTimelineStep key={index} index={index}>
          <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{step.text}</p>
          <EvidenceLinks sources={step.evidence} onOpenPair={onOpenPair} />
        </SopTimelineStep>)}
      </SopTimeline>
    </SopDetailSection>
    {!sameRetest && <SopDetailSection title="复测方法">
      <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{branch.retest}</p>
      <EvidenceLinks sources={branch.retest_evidence} onOpenPair={onOpenPair} />
    </SopDetailSection>}
    <SopDetailSection title={sameRetest ? '复测与结果' : '复测结果'} layout="result">
      {branch.result_evidence.map(source => <div key={`${source.qa_id}:${source.message_id}:${source.quote}`} className="space-y-1">
        <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{source.quote}</p>
        <time className="block text-xs tabular-nums text-muted-foreground">{displayTime(source.ts)}</time>
        <EvidenceLinks sources={[source]} onOpenPair={onOpenPair} />
      </div>)}
      {sameRetest && <EvidenceLinks sources={branch.retest_evidence.filter(source => !branch.result_evidence.some(result =>
        result.qa_id === source.qa_id && result.message_id === source.message_id))} onOpenPair={onOpenPair} />}
    </SopDetailSection>
    {branch.escalation.trim() && hasEvidence(branch.escalation_evidence) && <SopDetailSection title="升级处理">
      <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{branch.escalation}</p>
      <EvidenceLinks sources={branch.escalation_evidence} onOpenPair={onOpenPair} />
    </SopDetailSection>}
  </article>
}

function SopDetail({ entry, onOpenPair }: { entry: CompleteItem; onOpenPair: OpenEvidencePair }) {
  return <>
    <SopDetailHeader status="效果已验证" verified kind={[entry.item.platform, entry.item.module].filter(Boolean).join(' · ')}
      title={<><SheetTitle className="text-lg leading-7 [overflow-wrap:anywhere]">{entry.item.title}</SheetTitle>
        <SheetDescription className="sr-only">适用条件、处理步骤和复测结果。</SheetDescription></>} />
    <div className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain px-5 py-5 sm:px-7">
      {entry.branches.map((branch, index) => <SopBranch key={branch.id ?? index} title={entry.item.title} branch={branch} onOpenPair={onOpenPair} />)}
    </div>
  </>
}

function KnowledgeDetail({ item, onOpenPair }: { item: VocLibraryKnowledge; onOpenPair: OpenEvidencePair }) {
  async function copyAnswer() {
    try {
      await navigator.clipboard.writeText([item.question, `适用范围 ${item.scope}`, item.answer, ...item.cautions].filter(Boolean).join('\n'))
      toast.success('已复制知识。')
    } catch {
      toast.error('剪贴板暂不可用，可选中文字复制。')
    }
  }

  return <>
    <SopDetailHeader status={item.status === 'verified' ? '效果已验证' : '客服来源指引'} verified={item.status === 'verified'}
      kind={item.kind === 'troubleshooting_advice' ? '排查指引' : item.kind === 'configuration_guide' ? '配置指引' : '处理方案'}
      title={<><SheetTitle className="text-lg leading-7 [overflow-wrap:anywhere]">{item.question}</SheetTitle>
        <SheetDescription className="sr-only">客服处理方法、适用范围和来源会话。</SheetDescription></>} />
    <div className="min-h-0 flex-1 space-y-6 overflow-y-auto overscroll-contain px-5 py-5 sm:px-7">
      <SopDetailSection title="适用范围"><p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{item.scope}</p></SopDetailSection>
      <SopDetailSection title="处理方法">
        <p className="whitespace-pre-wrap text-sm leading-7 [overflow-wrap:anywhere]">{item.answer}</p>
        <Button type="button" variant="outline" size="sm" onClick={() => void copyAnswer()}><Copy />复制知识</Button>
      </SopDetailSection>
      {item.cautions.length > 0 && <SopDetailSection title="注意事项">
        {item.cautions.map((text, index) => <p key={index} className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{text}</p>)}
      </SopDetailSection>}
      <SopDetailSection title="来源会话">
        {item.sources.map(source => {
          const evidence = item.evidence.find(ref => ref.qa_id === source.qa_id)
          return <article key={source.qa_id} className="space-y-3 rounded-lg border bg-muted/15 px-4 py-4">
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
              <span>{[source.customer_name, source.staff_name].filter(Boolean).join(' · ') || '七鱼会话'}</span>
              <time>{displayTime(source.asked_at)}</time>
            </div>
            <p className="whitespace-pre-wrap text-sm leading-6 [overflow-wrap:anywhere]">{source.question}</p>
            {source.answer !== item.answer && <p className="whitespace-pre-wrap border-t pt-3 text-sm leading-6 [overflow-wrap:anywhere]">{source.answer}</p>}
            <Button type="button" variant="outline" size="sm" onClick={() => onOpenPair(
              normalizePair({ qa_id: source.qa_id, question: source.question, asked_at: source.asked_at }),
              evidence?.message_id || source.answer_message_ids[0],
            )} aria-label={`查看来源会话 ${source.qa_id}`}>查看上下文</Button>
          </article>
        })}
      </SopDetailSection>
    </div>
  </>
}

function KnowledgeResults({ data, fullList, onOpenPair }: { data: VocKnowledgeLibraryData; fullList: FullList; onOpenPair: OpenEvidencePair }) {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const rowTrigger = useRef<HTMLButtonElement | null>(null)
  const searchInput = useRef<HTMLInputElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const entries = useMemo(() => data.items.map(item => ({ item, branches: item.type === 'sop' ? item.branches.filter(completeBranch) : [] }))
    .filter(entry => entry.item.type === 'knowledge' || entry.branches.length), [data.items])
  const terms = search.normalize('NFKC').toLowerCase().split(/\s+/).filter(Boolean)
  const matches = entries.filter(({ item, branches }) => {
    if (!terms.length) return true
    const content = [item.title, ...(item.type === 'knowledge' ? [item.question, item.answer, item.scope, ...item.cautions] : [item.platform, item.module]), ...branches.flatMap(branch => [branch.condition, branch.retest,
      hasEvidence(branch.escalation_evidence) ? branch.escalation : '',
      ...branch.steps.map(step => step.text), ...branch.result_evidence.map(source => source.quote)])].join('\n').normalize('NFKC').toLowerCase()
    return terms.every(term => content.includes(term))
  })
  const total = fullList?.total ?? matches.length
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const currentPage = Math.min(page, pageCount)
  const pending = Boolean(fullList && (currentPage > 1 || terms.length))
  const start = (currentPage - 1) * pageSize
  const pages = pageCount <= 5 ? Array.from({ length: pageCount }, (_, index) => index + 1)
    : [...new Set([1, currentPage - 1, currentPage, currentPage + 1, pageCount])].filter(value => value >= 1 && value <= pageCount)
  const selected = entries.find(entry => entry.item.id === selectedId)

  function changePage(next: number) {
    if (currentPage === next) return
    setPage(next)
    if (next > 1) fullList?.request()
    heading.current?.focus({ preventScroll: true })
    heading.current?.scrollIntoView?.({ block: 'start' })
  }

  return <>
    <Card className="gap-0 overflow-hidden py-0">
      <div className="flex flex-wrap items-end justify-between gap-4 border-b bg-linear-to-b from-muted/40 to-card px-4 py-5 sm:px-6">
        <div className="space-y-1">
          <h2 ref={heading} tabIndex={-1} className="scroll-mt-4 text-base font-semibold tracking-tight focus:outline-none">客服知识与处理方案</h2>
          <p className="text-xs tabular-nums text-muted-foreground">{count(fullList?.total ?? entries.length)} 项沉淀知识</p>
        </div>
        {(entries.length > 0 || search) && <div className="relative w-full sm:w-64">
          <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input ref={searchInput} type="search" value={search} maxLength={200} placeholder="搜索问题、回复或适用范围" aria-label="搜索沉淀知识"
            className="bg-background pl-9" onChange={event => {
              const value = event.target.value
              setSearch(value); setPage(1)
              if (value.trim()) fullList?.request()
            }} />
        </div>}
      </div>
      {pending && fullList ? <QueryPanel loading={!fullList.error} error={fullList.error} retry={fullList.retry}>{null}</QueryPanel> : matches.length ? <>
        <div aria-hidden="true" className={`${rowColumns} hidden border-b bg-muted/25 px-6 py-2.5 text-xs text-muted-foreground lg:grid`}>
          <span>问题与处理方法</span><span>知识类型</span><span>最近更新</span><span />
        </div>
        <ul aria-label="沉淀知识" className="divide-y">{matches.slice(start, start + pageSize).map(({ item }) => {
          const kind = item.type === 'sop' ? '已验证 SOP' : item.status === 'verified' ? '已验证方案' : item.kind === 'troubleshooting_advice' ? '排查指引' : '配置指引'
          return <li key={item.id} className={`${rowColumns} grid px-4 py-3.5 sm:px-6`}>
            <div className="min-w-0 space-y-1">
              <p className="line-clamp-2 text-sm font-medium [overflow-wrap:anywhere]" title={item.title}>{item.title}</p>
              <p className="line-clamp-2 text-xs leading-5 text-muted-foreground [overflow-wrap:anywhere]">{item.type === 'knowledge' ? item.answer : [item.platform, item.module].filter(Boolean).join(' · ')}</p>
            </div>
            <span className="hidden text-xs text-muted-foreground lg:block">{kind}</span>
            <span className="hidden text-xs tabular-nums text-muted-foreground lg:block">{item.updated_at || '—'}</span>
            <Button type="button" variant="outline" size="sm" aria-haspopup="dialog" aria-label={`${item.type === 'sop' ? '查看 SOP' : '查看知识'} · ${item.title}`}
              onClick={event => { rowTrigger.current = event.currentTarget; setSelectedId(item.id) }}>{item.type === 'sop' ? '查看 SOP' : '查看知识'}</Button>
          </li>
        })}</ul>
      </> : <div className="flex min-h-44 flex-col items-center justify-center gap-2 px-6 text-center text-sm">
        <p className="font-medium">{terms.length ? '未找到匹配的知识' : '当前范围暂无可复用的客服知识'}</p>
        {terms.length > 0 && <Button type="button" variant="ghost" size="sm" onClick={() => { setSearch(''); setPage(1); searchInput.current?.focus() }}>清除搜索</Button>}
      </div>}
      {pageCount > 1 && !(pending && terms.length) && <div className="flex flex-wrap items-center justify-between gap-3 border-t bg-muted/15 px-4 py-3 text-xs text-muted-foreground sm:px-6">
        <span aria-live="polite" className="tabular-nums">{count(start + 1)}–{count(Math.min(start + pageSize, total))} / 共 {count(total)} 项</span>
        <nav aria-label="知识分页" className="flex flex-wrap items-center gap-1">
          <Button type="button" variant="ghost" size="icon-sm" aria-label="上一页" title="上一页" disabled={currentPage === 1} onClick={() => changePage(currentPage - 1)}><ChevronLeft /></Button>
          {pages.map((number, index) => <Fragment key={number}>
            {index > 0 && number - pages[index - 1] > 1 && <span aria-hidden="true" className="px-1">…</span>}
            <Button type="button" variant={currentPage === number ? 'outline' : 'ghost'} size="icon-sm" aria-label={`第 ${number} 页`}
              aria-current={currentPage === number ? 'page' : undefined} onClick={() => changePage(number)} className="tabular-nums">{number}</Button>
          </Fragment>)}
          <Button type="button" variant="ghost" size="icon-sm" aria-label="下一页" title="下一页" disabled={currentPage === pageCount} onClick={() => changePage(currentPage + 1)}><ChevronRight /></Button>
        </nav>
      </div>}
    </Card>
    <Sheet open={Boolean(selected)} onOpenChange={open => { if (!open) setSelectedId(null) }}>
      <SheetContent className="w-full gap-0 data-[state=closed]:duration-200 data-[state=open]:duration-250 sm:max-w-2xl" onCloseAutoFocus={event => {
        event.preventDefault(); (rowTrigger.current?.isConnected ? rowTrigger.current : searchInput.current)?.focus()
      }}>
        {selected?.item.type === 'knowledge' && <KnowledgeDetail item={selected.item} onOpenPair={onOpenPair} />}
        {selected?.item.type === 'sop' && <SopDetail entry={{ item: selected.item, branches: selected.branches }} onOpenPair={onOpenPair} />}
      </SheetContent>
    </Sheet>
  </>
}

export function VocKnowledgeLibrary({ filters, refreshKey = 0, onOpenPair }: Props) {
  const [revision, setRevision] = useState(0)
  const key = refreshKey + revision
  const sops = useVocFirstPageList<VocKnowledgeLibraryData>('voc2/knowledge-library', filters, key, revision === 0)
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  useBackgroundRefresh(refresh, false, sops.fetching, key, 120_000)
  const fullList = sops.data && sops.fullList && sops.fullList.total > sops.data.items.length ? sops.fullList : undefined
  return <QueryPanel hasData={sops.data !== null} loading={sops.loading} error={sops.error} retry={sops.retry}>
    {sops.data && <KnowledgeResults key={JSON.stringify(filters)} data={sops.data} fullList={fullList} onOpenPair={onOpenPair} />}
  </QueryPanel>
}

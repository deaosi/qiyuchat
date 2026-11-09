import { useState } from 'react'
import type { ApiFilters, VocTopic } from '@/lib/api'
import type { QAPair } from '@/lib/demo-data'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { KnowledgeWorkbench, type KnowledgeRequest } from './knowledge-workbench'
import { VocWorkbench } from './voc-workbench'

export function VocWorkspace({ filters, refreshKey, onOpenPair, onRuleClusters }: {
  filters: ApiFilters; refreshKey?: number; onOpenPair: (pair: QAPair) => void; onRuleClusters: () => void
}) {
  const [page, setPage] = useState('questions')
  const [request, setRequest] = useState<KnowledgeRequest | null>(null)
  function createKnowledge(topic: VocTopic) { setRequest({ topic, token: Date.now() }); setPage('knowledge') }
  return <Tabs value={page} onValueChange={setPage} className="gap-6">
    <TabsList variant="line" className="gap-5 border-b"><TabsTrigger value="questions" className="px-0">高频问题</TabsTrigger><TabsTrigger value="knowledge" className="px-0">知识沉淀</TabsTrigger></TabsList>
    <TabsContent value="questions" className="mt-0"><VocWorkbench filters={filters} refreshKey={refreshKey} onOpenPair={onOpenPair} onRuleClusters={onRuleClusters} onCreateKnowledge={createKnowledge} /></TabsContent>
    <TabsContent value="knowledge" className="mt-0"><KnowledgeWorkbench filters={filters} refreshKey={refreshKey} onOpenPair={onOpenPair} request={request} onRequestConsumed={() => setRequest(null)} onQuestions={() => setPage('questions')} /></TabsContent>
  </Tabs>
}

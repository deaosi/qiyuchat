import { useEffect, useRef, useState } from 'react'
import type { ApiFilters } from '@/lib/api'
import { prefetchApi } from '@/lib/api-prefetch'
import { requestActivity } from '@/lib/request-activity'
import { vocTopicPageFilters } from '@/lib/voc-first-page'

function firstPageRequests(view: string, filters: ApiFilters, knowledgeFilters: ApiFilters) {
  switch (view) {
    case 'overview':
      return [{ name: 'summary', filters }, { name: 'trend', filters }]
    case 'questions':
      return [{ name: 'voc2/topics', filters: vocTopicPageFilters({ ...filters, support_intent: 'all', diagnosis_status: 'all' }, 'all', 0) }]
    case 'voc-next':
      return [{ name: 'voc2/knowledge-library', filters: { ...knowledgeFilters, first_page: 1 as const } }]
    default:
      return []
  }
}

export function useSidebarPreload(view: string, filters: ApiFilters, refreshKey: number, paused: boolean, knowledgeFilters: ApiFilters) {
  const [target, setTarget] = useState<string | null>(null)
  const active = useRef<Promise<unknown> | null>(null)

  useEffect(() => {
    if (!target || target === view || paused) return
    const requests = firstPageRequests(target, filters, knowledgeFilters)
    if (!requests.length) return

    let cancelled = false
    let ready = false
    let waiting = false
    const next = () => {
      if (cancelled || !ready || !requests.length || document.visibilityState === 'hidden'
        || requestActivity.getSnapshot().foreground > 0) return
      if (active.current) {
        if (!waiting) {
          waiting = true
          void active.current.then(() => { waiting = false; next() })
        }
        return
      }
      const request = requests.shift()!
      const pending = prefetchApi(request.name, request.filters, refreshKey)
      active.current = pending
      // A navigation to this destination can join the running request.
      void pending.then(() => { active.current = null; next() })
    }
    const timer = window.setTimeout(() => { ready = true; next() }, 180)
    const unsubscribe = requestActivity.subscribe(next)
    document.addEventListener('visibilitychange', next)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      unsubscribe()
      document.removeEventListener('visibilitychange', next)
    }
  }, [target, view, filters, knowledgeFilters, refreshKey, paused])

  return {
    prepare: setTarget,
    cancel: (name: string) => setTarget(current => current === name ? null : current),
  }
}

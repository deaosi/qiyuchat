import { useEffect } from 'react'
import type { ApiFilters } from '@/lib/api'
import { prefetchApi } from '@/lib/api-prefetch'
import { requestActivity } from '@/lib/request-activity'
import { vocFrequencyScope, vocTopicPageFilters } from '@/lib/voc-first-page'

/** Warm the other VOC destinations only after the current view has settled. */
export function useVocFirstPagePreload(view: string, filters: ApiFilters, refreshKey: number, paused = false) {
  useEffect(() => {
    if (paused) return
    const frequency = vocFrequencyScope(filters)
    const requests: { endpoint: string; params: ApiFilters }[] = []
    if (view !== 'voc-next') requests.push({ endpoint: 'voc2/complete-sops', params: { ...filters, first_page: 1 } })
    if (view !== 'questions') requests.push(
      { endpoint: 'voc2/topics', params: vocTopicPageFilters(frequency, 'all', 0) },
      { endpoint: 'voc2/summary', params: frequency },
    )

    let cancelled = false
    let running = false
    let timer: number | undefined
    let idle: number | undefined
    const canStart = () => !cancelled && document.visibilityState !== 'hidden' && requestActivity.getSnapshot().foreground === 0
    const start = () => {
      idle = undefined
      if (!canStart()) return
      const request = requests.shift()
      if (!request) return
      running = true
      void prefetchApi(request.endpoint, request.params, refreshKey).finally(() => {
        running = false
        schedule()
      })
    }
    const schedule = () => {
      if (timer !== undefined || idle !== undefined || running || !requests.length || !canStart()) return
      timer = window.setTimeout(() => {
        timer = undefined
        if (!canStart()) return
        if (typeof window.requestIdleCallback === 'function') idle = window.requestIdleCallback(start, { timeout: 1_000 })
        else start()
      }, 350)
    }
    const unsubscribe = requestActivity.subscribe(schedule)
    document.addEventListener('visibilitychange', schedule)
    schedule()
    return () => {
      cancelled = true
      unsubscribe()
      document.removeEventListener('visibilitychange', schedule)
      if (timer !== undefined) window.clearTimeout(timer)
      if (idle !== undefined) window.cancelIdleCallback?.(idle)
    }
  }, [view, filters, refreshKey, paused])
}

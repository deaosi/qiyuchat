import { startTransition, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, apiGet, type ApiFilters } from '@/lib/api'
import { findPrefetch, rememberPrefetch } from '@/lib/api-prefetch'

/** A query never keeps rows from another source or filter scope on screen. */
export function useApi<T>(name: string, filters: ApiFilters, refreshKey = 0, enabled = true, usePrefetch = false) {
  const key = JSON.stringify(filters)
  const [state, setState] = useState<{ key: string; data: T | null; loading: boolean; error: string | null }>({ key: '', data: null, loading: enabled, error: null })
  const [retryKey, setRetryKey] = useState(0)
  const revision = useRef(0)
  const latest = useRef(state)
  latest.current = state
  const scopeKey = `${name}:${key}`
  const queryKey = `${scopeKey}:${refreshKey}:${retryKey}`
  const reusePrefetch = usePrefetch && retryKey === 0
  useEffect(() => {
    const controller = new AbortController()
    const current = ++revision.current
    if (!enabled) return () => controller.abort()
    // Revalidation keeps the mounted view. A different endpoint/filter scope
    // still hides the previous rows immediately, before the effect runs.
    const revalidating = latest.current.key === scopeKey
    if (revalidating) setState(previous => previous.key === scopeKey ? { ...previous, loading: true, error: null } : previous)
    const params = JSON.parse(key) as ApiFilters
    const load = async () => {
      const data = await apiGet<T>(name, params, controller.signal, { background: revalidating && latest.current.data !== null })
      if (usePrefetch && !controller.signal.aborted) rememberPrefetch(name, params, refreshKey, data)
      return data
    }
    const prefetched = reusePrefetch && findPrefetch(name, params, refreshKey)
    const request = prefetched ? prefetched.pending.then(result => {
      if (result) return result.data as T
      if (controller.signal.aborted) throw new DOMException('Request aborted', 'AbortError')
      return load()
    }) : load()
    request
      .then(data => {
        if (current === revision.current && !controller.signal.aborted) {
          startTransition(() => setState({ key: scopeKey, data, loading: false, error: null }))
        }
      })
      .catch((error: unknown) => {
        const invalidated = error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429
        if (current === revision.current && !controller.signal.aborted) setState(previous => ({ key: scopeKey, data: !invalidated && previous.key === scopeKey ? previous.data : null, loading: false, error: error instanceof Error ? error.message : '无法连接服务，请稍后重试。' }))
      })
    return () => controller.abort()
  }, [name, key, scopeKey, queryKey, enabled, reusePrefetch, usePrefetch, refreshKey])
  const retry = useCallback(() => setRetryKey(value => value + 1), [])
  const current = enabled && state.key === scopeKey
  const prepared = enabled && !current && reusePrefetch ? findPrefetch(name, filters, refreshKey)?.result : undefined
  return {
    data: current ? state.data : prepared ? prepared.data as T : null,
    loading: enabled && !prepared && (!current || (state.loading && state.data === null)),
    refreshing: current && state.loading && state.data !== null,
    error: current ? state.error : null,
    retry,
  }
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, apiGet, type ApiFilters } from '@/lib/api'

/** A query never keeps rows from another source or filter scope on screen. */
export function useApi<T>(name: string, filters: ApiFilters, refreshKey = 0, enabled = true) {
  const key = JSON.stringify(filters)
  const [state, setState] = useState<{ key: string; data: T | null; loading: boolean; error: string | null }>({ key: '', data: null, loading: enabled, error: null })
  const [retryKey, setRetryKey] = useState(0)
  const revision = useRef(0)
  const latest = useRef(state)
  latest.current = state
  const scopeKey = `${name}:${key}`
  const queryKey = `${scopeKey}:${refreshKey}:${retryKey}`
  useEffect(() => {
    const controller = new AbortController()
    const current = ++revision.current
    if (!enabled) return () => controller.abort()
    // Revalidation keeps the mounted view. A different endpoint/filter scope
    // still hides the previous rows immediately, before the effect runs.
    setState(previous => ({ key: scopeKey, data: previous.key === scopeKey ? previous.data : null, loading: true, error: null }))
    apiGet<T>(name, JSON.parse(key) as ApiFilters, controller.signal, { background: latest.current.key === scopeKey && latest.current.data !== null })
      .then(data => {
        if (current === revision.current && !controller.signal.aborted) setState({ key: scopeKey, data, loading: false, error: null })
      })
      .catch((error: unknown) => {
        const invalidated = error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429
        if (current === revision.current && !controller.signal.aborted) setState(previous => ({ key: scopeKey, data: !invalidated && previous.key === scopeKey ? previous.data : null, loading: false, error: error instanceof Error ? error.message : '无法连接服务，请稍后重试。' }))
      })
    return () => controller.abort()
  }, [name, key, scopeKey, queryKey, enabled])
  const retry = useCallback(() => setRetryKey(value => value + 1), [])
  const current = enabled && state.key === scopeKey
  return {
    data: current ? state.data : null,
    loading: enabled && (!current || (state.loading && state.data === null)),
    refreshing: current && state.loading && state.data !== null,
    error: current ? state.error : null,
    retry,
  }
}

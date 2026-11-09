import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet, type ApiFilters } from '@/lib/api'

/** A query never keeps rows from another source or filter scope on screen. */
export function useApi<T>(name: string, filters: ApiFilters, refreshKey = 0, enabled = true) {
  const key = JSON.stringify(filters)
  const [state, setState] = useState<{ key: string; data: T | null; loading: boolean; error: string | null }>({ key: '', data: null, loading: enabled, error: null })
  const [retryKey, setRetryKey] = useState(0)
  const revision = useRef(0)
  const queryKey = `${name}:${key}:${refreshKey}:${retryKey}`
  useEffect(() => {
    const controller = new AbortController()
    const current = ++revision.current
    if (!enabled) return () => controller.abort()
    setState({ key: queryKey, data: null, loading: true, error: null })
    apiGet<T>(name, JSON.parse(key) as ApiFilters, controller.signal)
      .then(data => {
        if (current === revision.current && !controller.signal.aborted) setState({ key: queryKey, data, loading: false, error: null })
      })
      .catch((error: unknown) => {
        if (current === revision.current && !controller.signal.aborted) setState({ key: queryKey, data: null, loading: false, error: error instanceof Error ? error.message : '无法连接服务，请稍后重试。' })
      })
    return () => controller.abort()
  }, [name, key, queryKey, enabled])
  const retry = useCallback(() => setRetryKey(value => value + 1), [])
  const current = enabled && state.key === queryKey
  return { data: current ? state.data : null, loading: enabled && (!current || state.loading), error: current ? state.error : null, retry }
}

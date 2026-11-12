import { apiGet, apiUrl, type ApiFilters } from './api'

type Result = { data: unknown }
type Entry = { expires: number; pending: Promise<Result | null>; result?: Result }

const entries = new Map<string, Entry>()
const ttl = 60_000
const maxEntries = 16

function cacheKey(name: string, filters: ApiFilters, revision: number) {
  const [path, query] = apiUrl(name, filters).split('?')
  const params = new URLSearchParams(query)
  params.sort()
  return `${path}?${params}:${revision}`
}

function store(key: string, entry: Entry) {
  entries.delete(key)
  for (const [previous, value] of entries) if (value.expires <= Date.now()) entries.delete(previous)
  if (entries.size >= maxEntries) {
    const oldest = entries.keys().next().value
    if (oldest) entries.delete(oldest)
  }
  entries.set(key, entry)
}

export function findPrefetch(name: string, filters: ApiFilters, revision: number): Entry | undefined {
  const entry = entries.get(cacheKey(name, filters, revision))
  return entry && entry.expires > Date.now() ? entry : undefined
}

export function prefetchApi(name: string, filters: ApiFilters, revision: number): Promise<Result | null> {
  const existing = findPrefetch(name, filters, revision)
  if (existing) return existing.pending
  const key = cacheKey(name, filters, revision)
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 15_000)
  const pending = apiGet<unknown>(name, filters, controller.signal, { prefetch: true })
    .then(data => {
      const result = { data }
      if (entries.get(key) === entry) {
        entry.result = result
        entry.expires = Date.now() + ttl
      }
      return result
    })
    .catch(() => {
      if (entries.get(key) === entry) entries.delete(key)
      return null
    })
    .finally(() => window.clearTimeout(timer))
  const entry: Entry = { expires: Date.now() + ttl, pending }
  store(key, entry)
  return pending
}

export function rememberPrefetch(name: string, filters: ApiFilters, revision: number, data: unknown) {
  const result = { data }
  store(cacheKey(name, filters, revision), { expires: Date.now() + ttl, pending: Promise.resolve(result), result })
}

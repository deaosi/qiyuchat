import type { ApiFilters } from '@/lib/api'
import { normalizeChatFilters } from '@/lib/voc-chat'

const topicIdPattern = /^t_[a-f0-9]{24}$/

export function topicHref(topicId: string, filters: ApiFilters): string {
  if (!topicIdPattern.test(topicId)) return '#questions'
  const { days, start, end } = normalizeChatFilters(filters)
  const params = new URLSearchParams({ topic: topicId })
  if (start) params.set('start', start)
  if (end) params.set('end', end)
  if (!start && !end) params.set('days', String(days ?? 7))
  return '#questions?' + params.toString()
}

export function readTopicLink(hash: string): { id: string; filters: ApiFilters } | undefined {
  const [route, query] = hash.split('?', 2)
  if (route !== '#questions' || !query) return undefined
  const params = new URLSearchParams(query)
  const id = params.get('topic') || ''
  if (!topicIdPattern.test(id) || params.getAll('topic').length !== 1) return undefined
  const raw = Object.fromEntries(params)
  const { days, start, end } = normalizeChatFilters(raw)
  if (start && end && start > end) return undefined
  return { id, filters: { source: 'local', days, start, end } }
}

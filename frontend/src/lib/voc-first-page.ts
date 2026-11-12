import type { ApiFilters } from './api'

export const vocTopicPageSize = 20

export function vocFrequencyScope(filters: ApiFilters): ApiFilters {
  return { ...filters, support_intent: 'all', diagnosis_status: 'all' }
}

export function vocTopicPageFilters(filters: ApiFilters, trend: NonNullable<ApiFilters['trend']>, offset: number): ApiFilters {
  return { ...filters, workbench: 1, trend, limit: vocTopicPageSize, offset }
}

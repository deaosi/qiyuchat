import type { ApiFilters } from './api'

export const vocTopicPageSize = 20

export function vocTopicPageFilters(filters: ApiFilters, trend: NonNullable<ApiFilters['trend']>, offset: number): ApiFilters {
  return { ...filters, workbench: 1, trend, limit: vocTopicPageSize, offset }
}

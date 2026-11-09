import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) { return twMerge(clsx(inputs)) }

export function customerLabel(name: string | null | undefined, id: string | null | undefined): string {
  const text = name?.trim() || ''
  const internal = /^(?:qiyu-(?:external(?:-sha256)?|user|session):|(?:anonymous|customer)-[0-9a-f]{16,64}$|[0-9a-f]{16,64}$)/i
  const unnamed = /^(?:匿名客户|身份未知访客|来源访客|未命名客户|客户|用户|访客)$/
  if (text && !internal.test(text) && !unnamed.test(text)) return text
  if (!id) return '匿名客户'
  // A stable display alias keeps anonymous rows distinguishable without
  // exposing the source identifier. Queries continue to use the real ID.
  let hash = 2166136261
  for (const char of id) hash = Math.imul(hash ^ char.codePointAt(0)!, 16777619)
  return `匿名客户 ${String((hash >>> 0) % 100000).padStart(5, '0')}`
}

export type RequestActivity = { foreground: number; background: number; label: string }

/** Shared by every transport so overlapping requests cannot end each other's indicator. */
export function createRequestActivity() {
  const requests = new Map<symbol, { label: string; background: boolean }>()
  const listeners = new Set<() => void>()
  let snapshot: RequestActivity = { foreground: 0, background: 0, label: '' }
  function publish() {
    const items = [...requests.values()]
    const foreground = items.filter(item => !item.background)
    snapshot = { foreground: foreground.length, background: items.length - foreground.length, label: foreground.at(-1)?.label || '' }
    listeners.forEach(listener => listener())
  }
  return {
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    getSnapshot: () => snapshot,
    getForegroundLabel: () => snapshot.foreground ? snapshot.label : null,
    getBusy: () => snapshot.foreground + snapshot.background > 0,
    begin(label: string, background = false) {
      const id = Symbol()
      requests.set(id, { label, background }); publish()
      return () => { if (requests.delete(id)) publish() }
    },
  }
}

export const requestActivity = createRequestActivity()

export async function withRequestActivity<T>(label: string, operation: () => Promise<T>, background = false): Promise<T> {
  const finish = requestActivity.begin(label, background)
  try { return await operation() }
  finally { finish() }
}

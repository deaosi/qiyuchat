import { useEffect, useRef, useState } from 'react'

/** Delay only the indicator, never the request or rendering of its result. */
export function useDelayedBusy(busy: boolean, delay = 120, minimum = 240) {
  const [visible, setVisible] = useState(false)
  const shownAt = useRef(0)
  useEffect(() => {
    if (busy && !visible) {
      const timer = window.setTimeout(() => { shownAt.current = Date.now(); setVisible(true) }, delay)
      return () => window.clearTimeout(timer)
    }
    if (!busy && visible) {
      const timer = window.setTimeout(() => setVisible(false), Math.max(0, minimum - (Date.now() - shownAt.current)))
      return () => window.clearTimeout(timer)
    }
  }, [busy, visible, delay, minimum])
  return visible
}

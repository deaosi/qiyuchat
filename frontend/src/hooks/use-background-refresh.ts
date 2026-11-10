import { useEffect, useState } from 'react'

/** Refresh after a request settles; hidden tabs and slow requests never overlap polls. */
export function useBackgroundRefresh(refresh: () => void, active: boolean, fetching: boolean, cycle = 0) {
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden')

  useEffect(() => {
    const onVisibility = () => {
      const next = document.visibilityState !== 'hidden'
      setVisible(next)
      if (next && !fetching) refresh()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [fetching, refresh])

  useEffect(() => {
    if (!visible || fetching) return
    const timer = window.setTimeout(refresh, active ? 5000 : 30000)
    return () => window.clearTimeout(timer)
  // A fast response can batch away the intermediate fetching render. The
  // request cycle still re-arms the timer instead of stopping after one poll.
  }, [active, fetching, refresh, visible, cycle])
}

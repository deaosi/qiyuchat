import React from 'react'
import ReactDOM from 'react-dom/client'
import '@fontsource-variable/geist'
import './index.css'
import App from './App'

// Retire only this application's old source preferences; keep display settings.
try {
  for (const key of Object.keys(localStorage)) {
    if (!/^qiyu(?:chat)?(?:[-_:]|$)/i.test(key)) continue
    const value = localStorage.getItem(key) || ''
    let parsed: unknown
    try { parsed = JSON.parse(value) } catch { parsed = value }
    if (parsed === 'demo') {
      localStorage.removeItem(key)
    } else if (parsed && typeof parsed === 'object') {
      let changed = false
      if ('source' in parsed && parsed.source === 'demo') { parsed.source = 'local'; changed = true }
      if ('filters' in parsed && parsed.filters && typeof parsed.filters === 'object'
        && 'source' in parsed.filters && parsed.filters.source === 'demo') {
        parsed.filters.source = 'local'; changed = true
      }
      if (changed) localStorage.setItem(key, JSON.stringify(parsed))
    }
  }
} catch { /* Storage may be unavailable. All requests still use business data. */ }

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)

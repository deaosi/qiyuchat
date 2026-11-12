import { createRoot } from 'react-dom/client'
import '@fontsource-variable/geist'
import '../guest/main.css'
import { VocGuestPage, type GuestPayload } from '@/components/qiyu/voc-guest-page'

const mount = document.getElementById('voc-guest-root')
const data = document.getElementById('voc-guest-data')
if (mount && data?.textContent) {
  const payload = JSON.parse(data.textContent) as GuestPayload
  document.title = `${payload.entry.title} · 语海Voc`
  createRoot(mount).render(<VocGuestPage {...payload} />)
}

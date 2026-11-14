import { createRoot } from 'react-dom/client'
import '@fontsource-variable/geist'
import '../guest/main.css'
import { VocGuestPage, type GuestPayload } from '@/components/qiyu/voc-guest-page'
import { VocGuestTicket, type GuestTicketPayload } from '@/components/qiyu/voc-guest-ticket'

const mount = document.getElementById('voc-guest-root')
const data = document.getElementById('voc-guest-data')
if (mount && data?.textContent) {
  const payload = JSON.parse(data.textContent) as GuestPayload | GuestTicketPayload
  const root = createRoot(mount)
  if ('ticket' in payload) {
    document.title = `${payload.ticket.title} · 语海Voc`
    root.render(<VocGuestTicket {...payload} />)
  } else {
    document.title = `${payload.entry.title} · 语海Voc`
    root.render(<VocGuestPage {...payload} />)
  }
}

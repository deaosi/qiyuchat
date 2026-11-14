const ticketIdPattern = /^[1-9][0-9]{0,18}$/

export function ticketHref(id: string): string {
  return ticketIdPattern.test(id) ? `#tickets?id=${id}` : '#tickets'
}

export function readTicketLink(hash: string): string | null {
  const [route, query] = hash.split('?', 2)
  if (route !== '#tickets' || !query) return null
  const ids = new URLSearchParams(query).getAll('id')
  return ids.length === 1 && ticketIdPattern.test(ids[0]) ? ids[0] : null
}

export async function readAdminImageFile(url: string) {
  const response = await fetch(`/api/detail/image?${new URLSearchParams({ url })}`, { credentials: 'same-origin', cache: 'no-store' })
  if (!response.ok) throw new Error('图片文件暂不可读取')
  const blob = await response.blob()
  if (!blob.size || !blob.type.startsWith('image/')) throw new Error('图片文件暂不可读取')
  const disposition = response.headers.get('Content-Disposition') || ''
  const filename = /(?:^|;)\s*filename\s*=\s*"?([a-z0-9_-][a-z0-9._-]*\.[a-z0-9]+)"?\s*(?:;|$)/i.exec(disposition)?.[1]
  const extension = new URL(url).pathname.match(/\.([a-z0-9]+)$/i)?.[1]
  return { blob, filename: filename || `会话图片${extension ? `.${extension}` : ''}` }
}

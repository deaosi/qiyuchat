import { useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ZoomableImage } from './zoomable-image'

type ImageFile = { blob: Blob; filename: string }
type ImageFileReader = (url: string) => Promise<ImageFile>
type ImageProps = { url: string; readFile?: ImageFileReader }

async function clipboardPng(blob: Blob): Promise<Blob> {
  if (blob.type === 'image/png') return blob
  const bitmap = await createImageBitmap(blob)
  try {
    const canvas = document.createElement('canvas')
    canvas.width = bitmap.width
    canvas.height = bitmap.height
    const context = canvas.getContext('2d')
    if (!context) throw new Error('无法转换图片')
    context.drawImage(bitmap, 0, 0)
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob(png => png ? resolve(png) : reject(new Error('无法转换图片')), 'image/png')
    })
  } finally {
    bitmap.close()
  }
}

function ImageFileActions({ url, readFile }: { url: string; readFile: ImageFileReader }) {
  const [pending, setPending] = useState<'copy' | 'download' | null>(null)
  const [feedback, setFeedback] = useState('')
  const file = useRef<Promise<ImageFile> | null>(null)

  function loadFile() {
    file.current ??= readFile(url).catch(error => {
      file.current = null
      throw error
    })
    return file.current
  }

  async function copy() {
    const clipboard = navigator.clipboard
    if (!clipboard?.write || typeof ClipboardItem === 'undefined') {
      setFeedback('当前浏览器无法直接复制，请右键图片选择「复制图片」。')
      return
    }
    setPending('copy')
    setFeedback('正在复制图片…')
    try {
      const png = loadFile().then(({ blob }) => clipboardPng(blob))
      // The clipboard call must start inside the click gesture while the bytes load.
      void png.catch(() => {})
      await clipboard.write([new ClipboardItem({ 'image/png': png })])
      setFeedback('图片已复制。')
    } catch {
      setFeedback('复制失败，请右键图片选择「复制图片」。')
    } finally {
      setPending(null)
    }
  }

  async function download() {
    setPending('download')
    setFeedback('正在读取图片文件…')
    try {
      const { blob, filename } = await loadFile()
      const objectUrl = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = objectUrl
      link.download = filename
      try {
        link.click()
      } finally {
        window.setTimeout(URL.revokeObjectURL.bind(URL, objectUrl), 1000)
      }
      setFeedback('已发起下载。')
    } catch {
      setFeedback('下载失败，请右键图片选择「图片另存为」。')
    } finally {
      setPending(null)
    }
  }

  return <>
    <Button type="button" variant="outline" size="sm" disabled={pending !== null} onClick={() => void copy()}>复制图片</Button>
    <Button type="button" variant="outline" size="sm" disabled={pending !== null} onClick={() => void download()}>下载原图</Button>
    <p role="status" className="text-xs leading-5 text-muted-foreground empty:sr-only">{feedback}</p>
  </>
}

export function ConversationImage({ url, readFile }: ImageProps) {
  return <ZoomableImage url={url} title="会话图片预览" alt="会话原图" viewportLabel="会话图片查看区"
    thumbnailAlt="会话图片" triggerLabel="放大会话图片" thumbnailClassName="w-fit max-w-[min(100%,14rem)]"
    imageClassName="max-h-36" showDimensions actions={loaded => readFile
      ? <ImageFileActions url={url} readFile={readFile} />
      : loaded && <Button asChild variant="outline" size="sm">
        <a href={url} download referrerPolicy="no-referrer">下载原图</a>
      </Button>} />
}

export function ConversationMessageBody({ text, imageUrls, readFile }: {
  text: string
  imageUrls: string[]
  readFile?: ImageFileReader
}) {
  return <>
    {(!imageUrls.length || text.trim() !== '[图片]') && <p>{text}</p>}
    {imageUrls.length > 0 && <div className="space-y-2 [&:not(:first-child)]:mt-2">
      {imageUrls.map((url, index) => <ConversationImage key={`${url}-${index}`} url={url} readFile={readFile} />)}
    </div>}
  </>
}

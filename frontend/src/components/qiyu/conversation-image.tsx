import { useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

type ImageFile = { blob: Blob; filename: string }
type ImageSize = { width: number; height: number }
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

function ImagePreview({ url, readFile }: ImageProps) {
  const [size, setSize] = useState<ImageSize | null>(null)
  const [originalSize, setOriginalSize] = useState(false)
  const [failed, setFailed] = useState(false)
  const [pending, setPending] = useState<'copy' | 'download' | null>(null)
  const [feedback, setFeedback] = useState('')
  const file = useRef<Promise<ImageFile> | null>(null)

  function loadFile() {
    if (!readFile) throw new Error('图片文件暂不可读取')
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

  const unavailable = readFile ? <p role="status" className="m-auto text-sm text-muted-foreground">图片暂不可查看</p>
    : <div className="m-auto space-y-2 text-center text-sm text-muted-foreground">
      <p role="status">图片暂不可查看</p>
      <Button variant="outline" size="sm" onClick={() => { setSize(null); setFailed(false) }}>重试加载图片</Button>
    </div>

  return <>
    <div className="border-b px-3 py-2.5 sm:px-4">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex items-center gap-3">
          <DialogTitle className="text-sm">会话图片预览</DialogTitle>
          {size && <span className="text-xs tabular-nums text-muted-foreground">{size.width} × {size.height}</span>}
          <DialogDescription className="hidden text-xs sm:block">右键可复制或另存</DialogDescription>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" disabled={!size || failed} onClick={() => setOriginalSize(value => !value)}>{originalSize ? '适应窗口' : '原尺寸'}</Button>
          {readFile ? <>
            <Button variant="outline" size="sm" disabled={pending !== null} onClick={() => void copy()}>复制图片</Button>
            <Button variant="outline" size="sm" disabled={pending !== null} onClick={() => void download()}>下载原图</Button>
          </> : size && !failed && <Button asChild variant="outline" size="sm">
            <a href={url} download referrerPolicy="no-referrer">下载原图</a>
          </Button>}
          <DialogClose asChild><Button variant="ghost" size="sm" aria-label="关闭图片预览">关闭</Button></DialogClose>
        </div>
      </div>
      <p role="status" className="mt-1 text-xs leading-5 text-muted-foreground empty:mt-0">{feedback}</p>
    </div>
    <div role="region" aria-label={originalSize ? '原尺寸图片，可滚动查看' : '完整图片'} tabIndex={originalSize ? 0 : undefined}
      className={cn('flex min-h-0 min-w-0 bg-muted/50', originalSize ? 'overflow-auto' : 'overflow-hidden p-3')}>
      {failed ? unavailable :
        <img src={url} alt="会话原图" decoding="async" referrerPolicy="no-referrer"
          onLoad={event => {
            const { naturalWidth: width, naturalHeight: height } = event.currentTarget
            if (width && height) setSize({ width, height })
          }} onError={() => setFailed(true)}
          className={cn('m-auto block h-auto w-auto shrink-0', originalSize ? 'max-h-none max-w-none' : 'max-h-full max-w-full object-contain')}
          style={originalSize && size ? { width: size.width, height: size.height } : undefined} />}
    </div>
  </>
}

export function ConversationImage({ url, readFile }: ImageProps) {
  const [failed, setFailed] = useState(false)
  if (failed) {
    if (readFile) return <p className="text-xs opacity-75">图片暂不可查看</p>
    return <div className="flex flex-wrap items-center gap-x-2">
      <p className="text-xs opacity-75">图片暂不可查看</p>
      <Button variant="ghost" size="xs" onClick={() => setFailed(false)}>重试加载图片</Button>
    </div>
  }

  return <Dialog>
    <DialogTrigger asChild>
      <button type="button" aria-label="放大会话图片" className="block w-fit max-w-[min(100%,14rem)] cursor-zoom-in overflow-hidden rounded-md focus-visible:outline-2 focus-visible:outline-offset-2">
        <img src={url} alt="会话图片" loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setFailed(true)}
          className="block h-auto max-h-36 max-w-full object-contain" />
      </button>
    </DialogTrigger>
    <DialogContent showCloseButton={false} className="h-[calc(100dvh-1rem)] w-[calc(100%-1rem)] max-w-none grid-rows-[auto_minmax(0,1fr)] gap-0 overflow-hidden p-0 sm:max-w-none">
      <ImagePreview url={url} readFile={readFile} />
    </DialogContent>
  </Dialog>
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

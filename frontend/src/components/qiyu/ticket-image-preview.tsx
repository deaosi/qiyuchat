import { useCallback, useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

const minimumZoom = 25
const defaultMaximumZoom = 1000
const padding = 12

type ImageSize = { width: number; height: number }
type ZoomPoint = { clientX: number; clientY: number; imageX: number; imageY: number }
type DragStart = { id: number; clientX: number; clientY: number; left: number; top: number }
type TicketImage = { url: string; name: string }

function ImageViewport({ url, name }: TicketImage) {
  const viewport = useRef<HTMLDivElement>(null)
  const image = useRef<HTMLImageElement>(null)
  const zoomPoint = useRef<ZoomPoint | null>(null)
  const drag = useRef<DragStart | null>(null)
  const currentZoom = useRef(100)
  const [zoom, setZoom] = useState(100)
  const [availableSize, setAvailableSize] = useState<ImageSize>({ width: 0, height: 0 })
  const [imageSize, setImageSize] = useState<ImageSize | null>(null)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const fit = imageSize && availableSize.width && availableSize.height
    ? Math.min(1, availableSize.width / imageSize.width, availableSize.height / imageSize.height)
    : 0
  const maximumZoom = fit ? Math.max(defaultMaximumZoom, Math.ceil(200 / fit)) : defaultMaximumZoom

  useLayoutEffect(() => {
    const element = viewport.current
    if (!element) return
    const measure = () => setAvailableSize({
      width: Math.max(1, element.offsetWidth - padding * 2),
      height: Math.max(1, element.offsetHeight - padding * 2),
    })
    measure()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }
    const observer = new ResizeObserver(measure)
    observer.observe(element, { box: 'border-box' })
    return () => observer.disconnect()
  }, [])

  const zoomAt = useCallback((value: number, x?: number, y?: number) => {
    const element = viewport.current
    const bounds = image.current?.getBoundingClientRect()
    const next = Math.min(maximumZoom, Math.max(minimumZoom, Math.round(value)))
    if (next === currentZoom.current) return
    if (element && bounds?.width && bounds.height) {
      const area = element.getBoundingClientRect()
      const clientX = x ?? area.left + area.width / 2
      const clientY = y ?? area.top + area.height / 2
      zoomPoint.current = {
        clientX, clientY,
        imageX: (clientX - bounds.left) / bounds.width,
        imageY: (clientY - bounds.top) / bounds.height,
      }
    }
    currentZoom.current = next
    setZoom(next)
  }, [maximumZoom])

  useLayoutEffect(() => {
    const element = viewport.current
    const point = zoomPoint.current
    const bounds = image.current?.getBoundingClientRect()
    if (!element || !point || !bounds) return
    element.scrollLeft += bounds.left + bounds.width * point.imageX - point.clientX
    element.scrollTop += bounds.top + bounds.height * point.imageY - point.clientY
    zoomPoint.current = null
  }, [zoom])

  useEffect(() => {
    const element = viewport.current
    if (!element || !fit || failed) return
    function wheel(event: WheelEvent) {
      if (!event.deltaY) return
      event.preventDefault()
      const units = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 240 : 1
      const delta = Math.max(-500, Math.min(500, event.deltaY * units))
      const next = Math.max(1, Math.abs(currentZoom.current * Math.expm1(-delta * 0.002)))
      zoomAt(currentZoom.current + (delta < 0 ? next : -next), event.clientX, event.clientY)
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [fit, failed, zoomAt])

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    const element = event.currentTarget
    if (event.pointerType !== 'mouse' || event.button !== 0 ||
        (element.scrollWidth <= element.clientWidth && element.scrollHeight <= element.clientHeight)) return
    const bounds = element.getBoundingClientRect()
    if (event.clientX >= bounds.left + element.clientWidth || event.clientY >= bounds.top + element.clientHeight) return
    drag.current = { id: event.pointerId, clientX: event.clientX, clientY: event.clientY, left: element.scrollLeft, top: element.scrollTop }
    element.setPointerCapture(event.pointerId)
  }

  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    const start = drag.current
    if (start?.id !== event.pointerId) return
    event.currentTarget.scrollLeft = start.left + start.clientX - event.clientX
    event.currentTarget.scrollTop = start.top + start.clientY - event.clientY
  }

  function fitImage() {
    zoomAt(100)
    viewport.current?.scrollTo(0, 0)
  }

  return <>
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-3 py-2 sm:px-4">
      <DialogTitle className="mr-auto shrink-0 text-sm">工单图片预览</DialogTitle>
      <DialogDescription className="sr-only">滚轮或滑块缩放，放大后可以拖动查看。</DialogDescription>
      <label className="flex items-center gap-2 text-xs">
        <span className="sr-only">缩放比例</span>
        <input type="range" min={minimumZoom} max={Math.max(maximumZoom, zoom)} value={zoom} disabled={!fit || failed}
          onChange={event => zoomAt(event.currentTarget.valueAsNumber)} aria-valuetext={`${zoom}%`}
          className="h-7 w-24 cursor-pointer accent-primary disabled:cursor-default sm:w-36" />
        <span className="w-11 text-right tabular-nums" aria-hidden="true">{zoom}%</span>
      </label>
      <Button type="button" variant="outline" size="sm" disabled={!fit || failed || zoom === 100} onClick={fitImage}>适应窗口</Button>
      <DialogClose asChild><Button type="button" variant="ghost" size="sm" aria-label="关闭图片预览">关闭</Button></DialogClose>
    </div>
    <div ref={viewport} role="region" aria-label="工单图片查看区" tabIndex={0}
      className={cn('min-h-0 min-w-0 overflow-auto bg-muted/50 outline-none [overflow-anchor:none] focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring',
        zoom > 100 && 'cursor-grab active:cursor-grabbing')}
      onPointerDown={startDrag} onPointerMove={moveDrag} onLostPointerCapture={() => { drag.current = null }}>
      <div className="grid h-max min-h-full w-max min-w-full place-items-center" style={{ padding }}>
        {failed ? <div className="space-y-2 text-center">
          <p role="status" className="text-sm text-muted-foreground">图片暂不可查看</p>
          <Button type="button" variant="outline" size="sm" onClick={() => { setFailed(false); setAttempt(value => value + 1) }}>重试加载图片</Button>
        </div> : <img key={attempt} ref={image} src={url} alt={name} draggable={false} decoding="async" referrerPolicy="no-referrer"
          onLoad={event => {
            const { naturalWidth: width, naturalHeight: height } = event.currentTarget
            if (width && height) setImageSize({ width, height })
          }}
          onError={() => { setImageSize(null); setFailed(true) }}
          className="block max-w-none select-none"
          style={imageSize && fit ? { width: imageSize.width * fit * zoom / 100, height: imageSize.height * fit * zoom / 100 }
            : { width: 0, height: 0 }} />}
      </div>
    </div>
  </>
}

export function TicketImagePreview({ url, name }: TicketImage) {
  const [failed, setFailed] = useState(false)
  const label = name || '工单截图'
  return <Dialog onOpenChange={open => { if (open && failed) setFailed(false) }}>
    <DialogTrigger asChild>
      <button type="button" aria-label={`查看图片：${label}`}
        className="block max-w-full cursor-zoom-in overflow-hidden rounded-md text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring">
        {failed ? <span className="text-sm text-muted-foreground">图片暂不可查看，点击重试</span> :
          <img src={url} alt={label} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setFailed(true)}
            className="block h-auto max-w-full rounded-md border bg-background object-contain" />}
      </button>
    </DialogTrigger>
    <DialogContent showCloseButton={false}
      className="h-[calc(100dvh-1rem)] w-[calc(100%-1rem)] max-w-none grid-rows-[auto_minmax(0,1fr)] gap-0 overflow-hidden p-0 sm:max-w-none">
      <ImageViewport url={url} name={label} />
    </DialogContent>
  </Dialog>
}

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

const minimumZoom = 25
const defaultMaximumZoom = 1000
const padding = 12

type ImageSize = { width: number; height: number }
type PointerPosition = { clientX: number; clientY: number }
type ZoomPoint = PointerPosition & { imageX: number; imageY: number }
type DragStart = PointerPosition & { id: number; left: number; top: number }
type PreviewProps = {
  url: string
  title: string
  alt: string
  viewportLabel: string
  showDimensions?: boolean
  actions?: (loaded: boolean) => ReactNode
}

function pinchPosition(touches: Map<number, PointerPosition>) {
  const [first, second] = touches.values()
  if (!first || !second) return null
  return {
    center: { clientX: (first.clientX + second.clientX) / 2, clientY: (first.clientY + second.clientY) / 2 },
    distance: Math.hypot(first.clientX - second.clientX, first.clientY - second.clientY),
  }
}

function ImageViewport({ url, title, alt, viewportLabel, showDimensions, actions }: PreviewProps) {
  const viewport = useRef<HTMLDivElement>(null)
  const image = useRef<HTMLImageElement>(null)
  const zoomPoint = useRef<ZoomPoint | null>(null)
  const drag = useRef<DragStart | null>(null)
  const touches = useRef(new Map<number, PointerPosition>())
  const pinchStart = useRef<{ distance: number; zoom: number } | null>(null)
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

  const zoomAt = useCallback((value: number, position?: PointerPosition, destination?: PointerPosition) => {
    const element = viewport.current
    const bounds = image.current?.getBoundingClientRect()
    const next = Math.min(maximumZoom, Math.max(minimumZoom, Math.round(value)))
    if (next === currentZoom.current) return false
    if (element && bounds?.width && bounds.height) {
      const area = element.getBoundingClientRect()
      const origin = position ?? { clientX: area.left + area.width / 2, clientY: area.top + area.height / 2 }
      const { clientX, clientY } = destination ?? origin
      zoomPoint.current = {
        clientX, clientY,
        imageX: (origin.clientX - bounds.left) / bounds.width,
        imageY: (origin.clientY - bounds.top) / bounds.height,
      }
    }
    currentZoom.current = next
    setZoom(next)
    return true
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
      zoomAt(currentZoom.current + (delta < 0 ? next : -next), event)
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [fit, failed, zoomAt])

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    const element = event.currentTarget
    if (event.pointerType === 'touch') {
      if (!fit || failed || touches.current.size >= 2) return
      touches.current.set(event.pointerId, { clientX: event.clientX, clientY: event.clientY })
      const pinch = pinchPosition(touches.current)
      if (pinch) pinchStart.current = { distance: Math.max(1, pinch.distance), zoom: currentZoom.current }
      element.setPointerCapture(event.pointerId)
      return
    }
    if (event.pointerType !== 'mouse' || event.button !== 0 ||
        (element.scrollWidth <= element.clientWidth && element.scrollHeight <= element.clientHeight)) return
    const bounds = element.getBoundingClientRect()
    if (event.clientX >= bounds.left + element.clientWidth || event.clientY >= bounds.top + element.clientHeight) return
    drag.current = { id: event.pointerId, clientX: event.clientX, clientY: event.clientY, left: element.scrollLeft, top: element.scrollTop }
    element.setPointerCapture(event.pointerId)
  }

  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    const element = event.currentTarget
    if (event.pointerType === 'touch') {
      const previous = touches.current.get(event.pointerId)
      if (!previous) return
      const position = { clientX: event.clientX, clientY: event.clientY }
      const previousPinch = pinchPosition(touches.current)
      touches.current.set(event.pointerId, position)
      const pinch = pinchPosition(touches.current)
      const start = pinchStart.current
      if (pinch && previousPinch && start &&
          zoomAt(start.zoom * pinch.distance / start.distance, previousPinch.center, pinch.center)) return
      const from = previousPinch?.center ?? previous
      const to = pinch?.center ?? position
      element.scrollLeft += from.clientX - to.clientX
      element.scrollTop += from.clientY - to.clientY
      return
    }
    const start = drag.current
    if (start?.id !== event.pointerId) return
    element.scrollLeft = start.left + start.clientX - event.clientX
    element.scrollTop = start.top + start.clientY - event.clientY
  }

  function finishDrag(event: PointerEvent<HTMLDivElement>) {
    if (touches.current.delete(event.pointerId)) pinchStart.current = null
    if (drag.current?.id === event.pointerId) drag.current = null
  }

  function fitImage() {
    zoomAt(100)
    if (viewport.current) {
      viewport.current.scrollLeft = 0
      viewport.current.scrollTop = 0
    }
  }

  return <>
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-3 py-2 sm:px-4">
      <div className="mr-auto flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1">
        <DialogTitle className="text-sm">{title}</DialogTitle>
        {showDimensions && imageSize && <span className="text-xs tabular-nums text-muted-foreground">{imageSize.width} × {imageSize.height}</span>}
      </div>
      <DialogDescription className="sr-only">滚轮、滑块或双指缩放，放大后可以拖动查看。右键可复制或另存。</DialogDescription>
      <label className="flex items-center gap-2 text-xs">
        <span className="sr-only">缩放比例</span>
        <input type="range" min={minimumZoom} max={Math.max(maximumZoom, zoom)} value={zoom} disabled={!fit || failed}
          onChange={event => zoomAt(event.currentTarget.valueAsNumber)} aria-valuetext={`${zoom}%`}
          className="h-7 w-24 cursor-pointer accent-primary disabled:cursor-default sm:w-36" />
        <span className="w-11 text-right tabular-nums" aria-hidden="true">{zoom}%</span>
      </label>
      <Button type="button" variant="outline" size="sm" disabled={!fit || failed || zoom === 100} onClick={fitImage}>适应窗口</Button>
      {actions?.(Boolean(imageSize) && !failed)}
      <DialogClose asChild><Button type="button" variant="ghost" size="sm" aria-label="关闭图片预览">关闭</Button></DialogClose>
    </div>
    <div ref={viewport} role="region" aria-label={viewportLabel} tabIndex={0}
      className={cn('min-h-0 min-w-0 overflow-auto bg-muted/50 outline-none [overflow-anchor:none] focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring',
        fit && !failed && 'touch-none',
        zoom > 100 && 'cursor-grab active:cursor-grabbing')}
      onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={finishDrag}
      onPointerCancel={finishDrag} onLostPointerCapture={finishDrag}>
      <div className="grid h-max min-h-full w-max min-w-full place-items-center" style={{ padding }}>
        {failed ? <div className="space-y-2 text-center">
          <p role="status" className="text-sm text-muted-foreground">图片暂不可查看</p>
          <Button type="button" variant="outline" size="sm" onClick={() => { setFailed(false); setAttempt(value => value + 1) }}>重试加载图片</Button>
        </div> : <img key={attempt} ref={image} src={url} alt={alt} draggable={false} decoding="async" referrerPolicy="no-referrer"
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

export function ZoomableImage({ thumbnailAlt, triggerLabel, thumbnailClassName, imageClassName, ...preview }: PreviewProps & {
  thumbnailAlt: string
  triggerLabel: string
  thumbnailClassName?: string
  imageClassName?: string
}) {
  const [failed, setFailed] = useState(false)
  if (failed) return <div className="flex flex-wrap items-center gap-x-2">
    <p className="text-xs text-muted-foreground">图片暂不可查看</p>
    <Button type="button" variant="ghost" size="xs" onClick={() => setFailed(false)}>重试加载图片</Button>
  </div>

  return <Dialog>
    <DialogTrigger asChild>
      <button type="button" aria-label={triggerLabel}
        className={cn('block max-w-full cursor-zoom-in overflow-hidden rounded-md text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring', thumbnailClassName)}>
        <img src={preview.url} alt={thumbnailAlt} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setFailed(true)}
          className={cn('block h-auto max-w-full object-contain', imageClassName)} />
      </button>
    </DialogTrigger>
    <DialogContent showCloseButton={false}
      className="h-[calc(100dvh-1rem)] w-[calc(100%-1rem)] max-w-none grid-rows-[auto_minmax(0,1fr)] gap-0 overflow-hidden p-0 sm:max-w-none">
      <ImageViewport {...preview} />
    </DialogContent>
  </Dialog>
}

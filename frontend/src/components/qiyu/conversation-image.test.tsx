import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ConversationImage } from './conversation-image'
import { readAdminImageFile } from './conversation-image-files'

const clipboardProperty = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
const url = 'https://ysf.nosdn.127.net/case-screen.jpg?variant=full'

function openPreview() {
  render(<ConversationImage url={url} readFile={readAdminImageFile} />)
  fireEvent.click(screen.getByRole('button', { name: '放大会话图片' }))
  return within(screen.getByRole('dialog', { name: '会话图片预览' }))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  if (clipboardProperty) Object.defineProperty(navigator, 'clipboard', clipboardProperty)
  else Reflect.deleteProperty(navigator, 'clipboard')
})

it('keeps the original available in the context menu and supports continuous zoom and fit reset', () => {
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(500)
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(400)
  const preview = openPreview()
  const viewport = preview.getByRole('region', { name: '会话图片查看区' })
  const zoom = preview.getByRole('slider', { name: '缩放比例' }) as HTMLInputElement
  const fit = preview.getByRole('button', { name: '适应窗口' }) as HTMLButtonElement
  Object.defineProperty(viewport, 'scrollTo', { value: vi.fn() })
  const image = preview.getByRole('img', { name: '会话原图' }) as HTMLImageElement
  Object.defineProperties(image, { naturalWidth: { value: 4000 }, naturalHeight: { value: 8000 } })
  fireEvent.load(image)
  expect(image.src).toBe(url)
  expect(fireEvent.contextMenu(image)).toBe(true)
  expect(preview.getByText('4000 × 8000')).toBeTruthy()
  const fitWidth = parseFloat(image.style.width)
  expect(fitWidth).toBeGreaterThan(0)
  expect(fitWidth).toBeLessThan(image.naturalWidth)
  expect(fit.disabled).toBe(true)

  fireEvent.change(zoom, { target: { value: '225' } })
  const zoomedWidth = parseFloat(image.style.width)
  expect(zoomedWidth).toBeGreaterThan(fitWidth)
  expect(zoom.valueAsNumber).toBe(225)
  fireEvent.wheel(viewport, { deltaY: -100 })
  expect(parseFloat(image.style.width)).toBeGreaterThan(zoomedWidth)

  fireEvent.click(fit)
  expect(parseFloat(image.style.width)).toBe(fitWidth)
  expect(zoom.valueAsNumber).toBe(100)
  expect(fit.disabled).toBe(true)
})

it('copies a PNG representation and downloads the same original file bytes with one request', async () => {
  const jpg = new Blob(['JPEG bytes'], { type: 'image/jpeg' })
  const png = new Blob(['PNG bytes'], { type: 'image/png' })
  const fetchImage = vi.fn(async () => ({
    ok: true, headers: new Headers({ 'Content-Disposition': 'inline; filename="case-screen.jpg"' }), blob: async () => jpg,
  }))
  vi.stubGlobal('fetch', fetchImage)
  vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ width: 4, height: 8, close: vi.fn() }))
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(png))
  class TestClipboardItem {
    constructor(readonly images: Record<string, Promise<Blob>>) {}
  }
  vi.stubGlobal('ClipboardItem', TestClipboardItem)
  const write = vi.fn(async ([item]: TestClipboardItem[]) => { expect(await item.images['image/png']).toBe(png) })
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write } })
  const createObjectURL = vi.fn().mockReturnValue('blob:https://qiyu.test/download')
  vi.stubGlobal('URL', class extends URL { static createObjectURL = createObjectURL; static revokeObjectURL = vi.fn() })
  const download = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { download(this.download, this.href) })

  const preview = openPreview()
  fireEvent.click(preview.getByRole('button', { name: '复制图片' }))
  await preview.findByText('图片已复制。')
  expect(write).toHaveBeenCalledOnce()
  fireEvent.click(preview.getByRole('button', { name: '下载原图' }))
  await preview.findByText('已发起下载。')
  expect(fetchImage).toHaveBeenCalledExactlyOnceWith(`/api/detail/image?${new URLSearchParams({ url })}`, { credentials: 'same-origin', cache: 'no-store' })
  expect(createObjectURL).toHaveBeenCalledExactlyOnceWith(jpg)
  expect(download).toHaveBeenCalledWith('case-screen.jpg', 'blob:https://qiyu.test/download')
})

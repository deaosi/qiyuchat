import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ConversationImage } from './conversation-image'

const clipboardProperty = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
const url = 'https://ysf.nosdn.127.net/case-screen.jpg?variant=full'

function openPreview() {
  render(<ConversationImage url={url} />)
  fireEvent.click(screen.getByRole('button', { name: '放大会话图片' }))
  return within(screen.getByRole('dialog', { name: '会话图片预览' }))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  if (clipboardProperty) Object.defineProperty(navigator, 'clipboard', clipboardProperty)
  else Reflect.deleteProperty(navigator, 'clipboard')
})

it('keeps the original image eligible for its native context menu and supports uncapped 1:1 scrolling', () => {
  const preview = openPreview()
  const image = preview.getByRole('img', { name: '会话原图' }) as HTMLImageElement
  Object.defineProperties(image, { naturalWidth: { value: 4000 }, naturalHeight: { value: 8000 } })
  fireEvent.load(image)
  expect(image.src).toBe(url)
  expect(fireEvent.contextMenu(image)).toBe(true)
  expect(preview.getByText('4000 × 8000')).toBeTruthy()
  expect(preview.getByRole('region', { name: '完整图片' })).toBeTruthy()
  expect(image.style.width).toBe('')

  fireEvent.click(preview.getByRole('button', { name: '原尺寸' }))
  expect(preview.getByRole('region', { name: '原尺寸图片，可滚动查看' }).classList.contains('overflow-auto')).toBe(true)
  expect(image.style.width).toBe('4000px')
  expect(image.style.height).toBe('8000px')

  fireEvent.click(preview.getByRole('button', { name: '适应窗口' }))
  expect(image.style.width).toBe('')
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

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ImportDialog } from './import-dialog'

const digest = 'a'.repeat(64)
const preview = {
  filename: '测试.csv', digest, kind: 'qa', fields: { question: '问题' }, total_rows: 1, valid_rows: 1,
  duplicate_rows: 0, skipped_rows: 0, errors: [], sessions: 1, estimated_pairs: 1, unknown_time: 0,
  samples: [{ question: '测试问题', answer: '测试回复', customer_name: '', group_name: '' }], can_import: true,
}

function selectFile() {
  const bytes = new Uint8Array([1, 2, 3]).buffer
  const file = new File([bytes], preview.filename)
  const read = vi.fn().mockResolvedValue(bytes)
  Object.defineProperty(file, 'arrayBuffer', { value: read })
  vi.stubGlobal('crypto', { subtle: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).fill(0xaa).buffer) } })
  fireEvent.change(screen.getByLabelText('选择 CSV / XLSX 文件'), { target: { files: [file] } })
  fireEvent.click(screen.getByRole('button', { name: '预览文件' }))
  return { bytes, read }
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('conversation import preview and confirmation', () => {
  it('uploads exactly the previewed bytes with the matching digest after confirmation', async () => {
    const fetch = vi.fn((_url: string, _options: RequestInit) => Promise.resolve(Response.json({})))
      .mockResolvedValueOnce(Response.json(preview)).mockResolvedValueOnce(Response.json({ new_sessions: 1, updated_sessions: 0, qa_pairs: 1 }))
    vi.stubGlobal('fetch', fetch)
    const imported = vi.fn()
    render(<ImportDialog open onOpenChange={vi.fn()} onImported={imported} />)
    const { bytes, read } = selectFile()
    await screen.findByText('字段识别')
    fireEvent.click(screen.getByRole('button', { name: '确认导入并清洗' }))
    await vi.waitFor(() => expect(imported).toHaveBeenCalledOnce())

    expect(read).toHaveBeenCalledOnce()
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(['/api/import/preview?source=local', '/api/import?source=local'])
    expect(fetch.mock.calls.every(([, request]) => request.body === bytes)).toBe(true)
    expect(new Headers(fetch.mock.calls[1][1].headers).get('X-File-Digest')).toBe(digest)
  })

  it.each([
    { response: { filename: preview.filename, digest, can_import: true }, error: '导入预览返回的数据不完整，请重新预览。' },
    { response: { ...preview, digest: 'b'.repeat(64) }, error: '文件校验不一致，请重新选择文件并预览。' },
  ])('keeps invalid preview data out of the confirmation view', async ({ response, error }) => {
    const fetch = vi.fn().mockResolvedValue(Response.json(response))
    vi.stubGlobal('fetch', fetch)
    render(<ImportDialog open onOpenChange={vi.fn()} onImported={vi.fn()} />)
    selectFile()

    expect((await screen.findByRole('alert')).textContent).toBe(error)
    expect(screen.queryByText('字段识别')).toBeNull()
    expect((screen.getByRole('button', { name: '确认导入并清洗' }) as HTMLButtonElement).disabled).toBe(true)
    expect(fetch).toHaveBeenCalledOnce()
  })
})

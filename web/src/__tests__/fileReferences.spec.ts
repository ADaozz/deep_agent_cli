import { expect, it } from 'vitest'
import { checkFileReferences, fileReferenceNotice, isImagePath } from '../store/fileReferences'
it('checks image suffixes case-insensitively while ordinary files remain usable', () => {
  for (const path of ['./a.PNG','/tmp/有 空格.JpEg','photo.jpg','a.webp','a.GIF']) expect(isImagePath(path)).toBe(true)
  for (const path of ['a.pdf','/a.png/document.txt','file','a.png.txt','.png']) expect(isImagePath(path)).toBe(false)
  expect(checkFileReferences(['./text.txt'], false)).toBe(true)
  expect(checkFileReferences(['./image.PNG'], false)).toBe(false)
  expect(fileReferenceNotice.value).toBe('请切换到支持图片理解的模型')
  expect(checkFileReferences(['./image.PNG'], true)).toBe(true)
  expect(fileReferenceNotice.value).toBe('')
})

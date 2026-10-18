import { ref } from 'vue'
import rules from '../../../agent/server/file_reference_rules.json'

export const fileReferenceNotice = ref('')
export const imageModelRequiredMessage = rules.image_model_required_message
export function notifyFileReference(message = imageModelRequiredMessage): void { fileReferenceNotice.value = message }
export function isImagePath(path: string): boolean {
  const name = path.trim().replace(/\\/g, '/').split('/').pop() || ''
  const dot = name.lastIndexOf('.')
  return dot > 0 && rules.image_extensions.includes(name.slice(dot).toLowerCase())
}
export function checkFileReferences(paths: string[], supportsImages: boolean): boolean {
  if (!supportsImages && paths.some(isImagePath)) { notifyFileReference(); return false }
  fileReferenceNotice.value = ''
  return true
}

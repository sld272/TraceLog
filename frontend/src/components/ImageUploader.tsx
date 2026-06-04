import { useRef, useState } from 'react'
import { type Attachment, uploadAttachment } from '@/api/client'
import { ImageGrid } from './ImageGrid'
import { ImageIcon } from '@/components/icons'
import { IMAGE_UPLOAD } from '@/utils/constants'
import styles from './ImageUploader.module.css'

interface ImageUploaderProps {
  attachments: Attachment[]
  disabled?: boolean
  compact?: boolean
  showPreview?: boolean
  showControls?: boolean
  onChange: (attachments: Attachment[]) => void
}

export function ImageUploader({
  attachments,
  disabled = false,
  compact = false,
  showPreview = true,
  showControls = true,
  onChange,
}: ImageUploaderProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const hasPreview = showPreview && attachments.length > 0

  if (!hasPreview && !showControls) {
    return null
  }

  const handleFiles = async (files: FileList | null) => {
    if (!files || disabled) return
    setError(null)
    const nextFiles = Array.from(files)
    if (attachments.length + nextFiles.length > IMAGE_UPLOAD.MAX_COUNT) {
      setError(`最多上传 ${IMAGE_UPLOAD.MAX_COUNT} 张图片`)
      return
    }
    const invalid = nextFiles.find((file) => !isAllowedImageFile(file))
    if (invalid) {
      setError('仅支持 JPEG/PNG 图片')
      return
    }

    setUploading(true)
    try {
      const uploaded = await Promise.all(nextFiles.map((file) => uploadAttachment(file)))
      onChange([...attachments, ...uploaded])
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
      if (inputRef.current) {
        inputRef.current.value = ''
      }
    }
  }

  const removeAttachment = (id: string) => {
    onChange(attachments.filter((attachment) => attachment.id !== id))
  }

  return (
    <div className={`${styles.uploader} ${compact ? styles.compact : ''}`}>
      {hasPreview && (
        <div className={styles.preview}>
          <ImageGrid
            attachments={attachments}
            compact
            disabled={disabled || uploading}
            onRemove={(attachment) => removeAttachment(attachment.id)}
          />
        </div>
      )}

      {showControls && (
        <div className={styles.controls}>
          <input
            ref={inputRef}
            type="file"
            accept="image/jpeg,image/png,.jpg,.jpeg,.png,.JPG,.JPEG,.PNG"
            multiple
            className={styles.input}
            onChange={(event) => handleFiles(event.target.files)}
            disabled={disabled || uploading || attachments.length >= IMAGE_UPLOAD.MAX_COUNT}
          />
          <button
            type="button"
            className={styles.pickButton}
            onClick={() => inputRef.current?.click()}
            disabled={disabled || uploading || attachments.length >= IMAGE_UPLOAD.MAX_COUNT}
            aria-label={uploading ? '图片上传中' : '添加图片'}
          >
            <ImageIcon />
          </button>
          {error && <span className={styles.error}>{error}</span>}
        </div>
      )}
    </div>
  )
}

function isAllowedImageFile(file: File) {
  const mediaType = file.type.split(';', 1)[0]?.trim().toLowerCase()
  if (mediaType && IMAGE_UPLOAD.ALLOWED_TYPES.has(mediaType)) {
    return true
  }
  const extension = file.name.split('.').pop()?.trim().toLowerCase()
  return Boolean(extension && IMAGE_UPLOAD.ALLOWED_EXTENSIONS.has(extension))
}

import { useRef, useState } from 'react'
import { type Attachment, attachmentUrl, uploadAttachment } from '@/api/client'
import { ImageGrid } from './ImageGrid'
import styles from './ImageUploader.module.css'

const MAX_IMAGES = 9
const MAX_IMAGE_BYTES = 5 * 1024 * 1024
const ALLOWED_TYPES = new Set(['image/jpeg', 'image/png'])

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
    if (attachments.length + nextFiles.length > MAX_IMAGES) {
      setError(`最多上传 ${MAX_IMAGES} 张图片`)
      return
    }
    const invalid = nextFiles.find((file) => !ALLOWED_TYPES.has(file.type) || file.size > MAX_IMAGE_BYTES)
    if (invalid) {
      setError('仅支持 5MB 以内的 JPEG/PNG 图片')
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
          <ImageGrid attachments={attachments} compact />
          <div className={styles.removeList}>
            {attachments.map((attachment) => (
              <button
                key={attachment.id}
                type="button"
                className={styles.removeButton}
                onClick={() => removeAttachment(attachment.id)}
                disabled={disabled || uploading}
                aria-label={`移除 ${attachment.original_filename ?? '图片'}`}
              >
                <img src={attachmentUrl(attachment)} alt="" />
                <span>×</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {showControls && (
        <div className={styles.controls}>
          <input
            ref={inputRef}
            type="file"
            accept="image/jpeg,image/png"
            multiple
            className={styles.input}
            onChange={(event) => handleFiles(event.target.files)}
            disabled={disabled || uploading || attachments.length >= MAX_IMAGES}
          />
          <button
            type="button"
            className={styles.pickButton}
            onClick={() => inputRef.current?.click()}
            disabled={disabled || uploading || attachments.length >= MAX_IMAGES}
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

function ImageIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
      <circle cx="8.5" cy="8.5" r="1.5" />
      <path d="M21 15l-5-5L5 21" />
    </svg>
  )
}

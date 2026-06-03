import { useState } from 'react'
import { type Attachment, attachmentUrl } from '@/api/client'
import { ImageViewer } from './ImageViewer'
import styles from './ImageGrid.module.css'

interface ImageGridProps {
  attachments: Attachment[]
  compact?: boolean
  disabled?: boolean
  onRemove?: (attachment: Attachment) => void
}

export function ImageGrid({ attachments, compact = false, disabled = false, onRemove }: ImageGridProps) {
  const [activeIndex, setActiveIndex] = useState<number | null>(null)
  if (attachments.length === 0) return null

  const visibleCount = Math.min(attachments.length, 9)
  const layout = getLayoutClass(visibleCount)

  return (
    <>
      <div className={`${styles.grid} ${layout} ${compact ? styles.compact : ''}`}>
        {attachments.slice(0, 9).map((attachment, index) => (
          <div key={attachment.id} className={styles.item}>
            <button
              type="button"
              className={styles.openButton}
              onClick={() => setActiveIndex(index)}
              aria-label="查看图片"
            >
              <img src={attachmentUrl(attachment)} alt="" loading="lazy" />
            </button>
            {onRemove && (
              <button
                type="button"
                className={styles.removeButton}
                disabled={disabled}
                aria-label={`移除 ${attachment.original_filename ?? '图片'}`}
                onClick={(event) => {
                  event.stopPropagation()
                  onRemove(attachment)
                }}
              >
                ×
              </button>
            )}
          </div>
        ))}
      </div>
      {activeIndex !== null && (
        <ImageViewer
          attachments={attachments}
          initialIndex={activeIndex}
          onClose={() => setActiveIndex(null)}
        />
      )}
    </>
  )
}

function getLayoutClass(count: number): string {
  if (count === 1) return styles.single ?? ''
  if (count === 2) return styles.row2 ?? ''
  if (count === 3) return styles.row3 ?? ''
  if (count === 4) return styles.grid2 ?? ''
  return styles.grid3 ?? ''
}

import { useState } from 'react'
import { type Attachment, attachmentUrl } from '@/api/client'
import { ImageViewer } from './ImageViewer'
import styles from './ImageGrid.module.css'

interface ImageGridProps {
  attachments: Attachment[]
  compact?: boolean
}

export function ImageGrid({ attachments, compact = false }: ImageGridProps) {
  const [activeIndex, setActiveIndex] = useState<number | null>(null)
  if (attachments.length === 0) return null

  const layout = attachments.length === 1
    ? styles.single
    : attachments.length <= 3
      ? styles.row
      : attachments.length <= 6
        ? styles.grid2
        : styles.grid3

  return (
    <>
      <div className={`${styles.grid} ${layout} ${compact ? styles.compact : ''}`}>
        {attachments.slice(0, 9).map((attachment, index) => (
          <button
            key={attachment.id}
            type="button"
            className={styles.item}
            onClick={() => setActiveIndex(index)}
            aria-label="查看图片"
          >
            <img src={attachmentUrl(attachment)} alt="" loading="lazy" />
          </button>
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

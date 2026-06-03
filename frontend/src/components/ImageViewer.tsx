import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { type Attachment, attachmentUrl } from '@/api/client'
import styles from './ImageViewer.module.css'

interface ImageViewerProps {
  attachments: Attachment[]
  initialIndex: number
  onClose: () => void
}

export function ImageViewer({ attachments, initialIndex, onClose }: ImageViewerProps) {
  const [index, setIndex] = useState(initialIndex)
  const current = attachments[index]

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      if (event.key === 'ArrowLeft') setIndex((value) => Math.max(0, value - 1))
      if (event.key === 'ArrowRight') setIndex((value) => Math.min(attachments.length - 1, value + 1))
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [attachments.length, onClose])

  if (!current) return null

  return createPortal(
    <div className={styles.overlay} onClick={onClose} role="dialog" aria-modal="true">
      <button className={styles.closeButton} type="button" onClick={onClose} aria-label="关闭">
        ×
      </button>
      {attachments.length > 1 && (
        <button
          className={`${styles.navButton} ${styles.prevButton}`}
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            setIndex((value) => Math.max(0, value - 1))
          }}
          disabled={index === 0}
          aria-label="上一张"
        >
          ‹
        </button>
      )}
      <img
        className={styles.image}
        src={attachmentUrl(current)}
        alt={current.original_filename ?? ''}
        onClick={(event) => event.stopPropagation()}
      />
      {attachments.length > 1 && (
        <button
          className={`${styles.navButton} ${styles.nextButton}`}
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            setIndex((value) => Math.min(attachments.length - 1, value + 1))
          }}
          disabled={index === attachments.length - 1}
          aria-label="下一张"
        >
          ›
        </button>
      )}
      <div className={styles.counter}>{index + 1}/{attachments.length}</div>
    </div>,
    document.body,
  )
}

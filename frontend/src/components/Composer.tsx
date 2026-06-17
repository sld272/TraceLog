import { useState, useRef, useEffect } from 'react'
import { type Attachment } from '@/api/client'
import { ImageUploader } from './ImageUploader'
import { LoadingDots, SendIcon } from '@/components/icons'
import { LAYOUT } from '@/utils/constants'
import styles from './Composer.module.css'

interface ComposerProps {
  onSubmit: (content: string, attachments: Attachment[]) => Promise<void>
  disabled?: boolean
  disabledReason?: string
}

export function Composer({ onSubmit, disabled = false, disabledReason }: ComposerProps) {
  const [content, setContent] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  /* Auto-resize textarea */
  useEffect(() => {
    const el = textareaRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, LAYOUT.TEXTAREA_MAX_HEIGHT)}px`
    }
  }, [content])

  const handleSubmit = async () => {
    const trimmed = content.trim()
    if ((!trimmed && attachments.length === 0) || submitting || disabled) return
    setSubmitting(true)
    setSubmitError(null)
    try {
      await onSubmit(trimmed, attachments)
      setContent('')
      setAttachments([])
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : '发布失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    /* Enter 发帖，Shift+Enter 换行；输入法组词时的 Enter 不触发发帖 */
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      handleSubmit()
    }
  }

  return (
    <div className={styles.composer}>
      <textarea
        ref={textareaRef}
        className={styles.textarea}
        value={content}
        onChange={(e) => setContent(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="写下你的想法..."
        rows={2}
        disabled={submitting || disabled}
        aria-label="发帖内容"
      />
      {disabled && disabledReason && (
        <div className={styles.disabledNotice}>
          {disabledReason}
        </div>
      )}
      {submitError && (
        <div className={styles.submitError} role="alert">
          {submitError}
        </div>
      )}
      <ImageUploader
        attachments={attachments}
        disabled={submitting || disabled}
        onChange={setAttachments}
        showControls={false}
      />
      <div className={styles.footer}>
        {(content.length > 0 || attachments.length > 0) && (
          <span className={styles.hint}>
            {content.length} 字{attachments.length > 0 ? ` · ${attachments.length} 图` : ''}
          </span>
        )}
        <div className={styles.actions}>
          <ImageUploader
            attachments={attachments}
            disabled={submitting || disabled}
            onChange={setAttachments}
            showPreview={false}
          />
          <span className={`${styles.submitWrap} kbdTip`}>
            <button
              className={styles.submitBtn}
              onClick={handleSubmit}
              disabled={(!content.trim() && attachments.length === 0) || submitting || disabled}
              aria-label="发布"
            >
              {submitting ? (
                <LoadingDots />
              ) : (
                <SendIcon />
              )}
            </button>
            <span className="kbdTipBubble" role="tooltip">
              发送 <span className="kbdTipKey">Enter</span>
            </span>
          </span>
        </div>
      </div>
    </div>
  )
}

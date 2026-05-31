import { useState, useRef, useEffect } from 'react'
import styles from './Composer.module.css'

interface ComposerProps {
  onSubmit: (content: string) => Promise<void>
}

export function Composer({ onSubmit }: ComposerProps) {
  const [content, setContent] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  /* Auto-resize textarea */
  useEffect(() => {
    const el = textareaRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, 200)}px`
    }
  }, [content])

  const handleSubmit = async () => {
    const trimmed = content.trim()
    if (!trimmed || submitting) return
    setSubmitting(true)
    try {
      await onSubmit(trimmed)
      setContent('')
    } finally {
      setSubmitting(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
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
        disabled={submitting}
        aria-label="发帖内容"
      />
      <div className={styles.footer}>
        <span className={styles.hint}>
          {content.length > 0 ? `${content.length} 字` : 'Cmd+Enter 发送'}
        </span>
        <button
          className={styles.submitBtn}
          onClick={handleSubmit}
          disabled={!content.trim() || submitting}
          aria-label="发布"
        >
          {submitting ? (
            <LoadingDots />
          ) : (
            <SendIcon />
          )}
        </button>
      </div>
    </div>
  )
}

function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  )
}

function LoadingDots() {
  return (
    <span className={styles.dots}>
      <span className={styles.dot} />
      <span className={styles.dot} />
      <span className={styles.dot} />
    </span>
  )
}

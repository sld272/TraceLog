import { useEffect, useState } from 'react'
import {
  type Suggestion,
  acceptSuggestion,
  dismissSuggestion,
} from '@/api/client'
import { StarIcon } from '@/components/icons'
import styles from './InlineSuggestions.module.css'

interface InlineSuggestionsProps {
  suggestions: Suggestion[]
  /** 私聊来源：采纳后目标对所有伙伴可见，需在卡片底部轻提示。公开场景不传。 */
  fromPrivateChat?: boolean
}

export function InlineSuggestions({ suggestions, fromPrivateChat }: InlineSuggestionsProps) {
  const [pending, setPending] = useState(suggestions)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const suggestionKey = suggestions.map((item) => item.id).join('|')

  useEffect(() => {
    setPending(suggestions)
    setError(null)
  }, [suggestionKey])

  if (pending.length === 0) return null

  const decide = async (suggestion: Suggestion, action: 'accept' | 'dismiss') => {
    setBusyId(suggestion.id)
    setError(null)
    try {
      if (action === 'accept') {
        await acceptSuggestion(suggestion.id)
      } else {
        await dismissSuggestion(suggestion.id)
      }
      setPending((current) => current.filter((item) => item.id !== suggestion.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : '处理失败')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className={styles.stack}>
      {pending.map((suggestion) => {
        const busy = busyId === suggestion.id
        return (
          <div key={suggestion.id} className={styles.card}>
            <span className={styles.icon} aria-hidden>
              <StarIcon width={15} height={15} />
            </span>
            <div className={styles.body}>
              <span className={styles.kicker}>记进目标？</span>
              <p className={styles.title}>{suggestionTitle(suggestion)}</p>
            </div>
            <div className={styles.actions}>
              <button
                className={styles.accept}
                disabled={busy}
                onClick={() => void decide(suggestion, 'accept')}
              >
                采纳
              </button>
              <button
                className={styles.dismiss}
                disabled={busy}
                onClick={() => void decide(suggestion, 'dismiss')}
              >
                忽略
              </button>
            </div>
          </div>
        )
      })}
      {fromPrivateChat && (
        <p className={styles.visibilityHint}>采纳后对所有伙伴可见</p>
      )}
      {error && <p className={styles.error}>{error}</p>}
    </div>
  )
}

function suggestionTitle(suggestion: Suggestion): string {
  const value = suggestion.payload.title
  return typeof value === 'string' && value.trim() ? value : '未命名建议'
}

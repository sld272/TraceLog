import { useEffect, useState } from 'react'
import {
  type Suggestion,
  acceptSuggestion,
  dismissSuggestion,
} from '@/api/client'
import { CheckIcon, StarIcon } from '@/components/icons'
import { formatDueDate } from '@/utils/date'
import styles from './InlineSuggestions.module.css'

interface InlineSuggestionsProps {
  suggestions: Suggestion[]
  /** 私聊来源：采纳后目标/待办对所有伙伴可见，需在卡片底部轻提示。公开场景不传。 */
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
        const schedule = suggestionSchedule(suggestion)
        return (
          <div key={suggestion.id} className={styles.card}>
            <span className={styles.icon} aria-hidden>
              {suggestion.kind === 'goal'
                ? <StarIcon width={15} height={15} />
                : <CheckIcon width={15} height={15} />}
            </span>
            <div className={styles.body}>
              <span className={styles.kicker}>{suggestionQuestion(suggestion)}</span>
              <p className={styles.title}>{suggestionTitle(suggestion)}</p>
              {schedule && <span className={styles.meta}>{schedule}</span>}
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
  const value = suggestion.kind === 'goal' ? suggestion.payload.title : suggestion.payload.task
  return typeof value === 'string' && value.trim() ? value : '未命名建议'
}

/** 待办建议的日期/时间摘要（含星期，便于采纳前核对）；goal 无日期返回 null。 */
function suggestionSchedule(suggestion: Suggestion): string | null {
  if (suggestion.kind !== 'todo') return null
  const { payload } = suggestion
  const date = typeof payload.date === 'string' ? payload.date.trim() : ''
  const start = typeof payload.start_time === 'string' ? payload.start_time.trim() : ''
  const end = typeof payload.end_time === 'string' ? payload.end_time.trim() : ''
  const time = [start, end].filter(Boolean).join(' - ')
  const dateText = date ? formatDueDate(date) : ''
  return [dateText, time].filter(Boolean).join(' ') || null
}

function suggestionQuestion(suggestion: Suggestion): string {
  if (suggestion.kind === 'goal') return '记进目标？'
  if (suggestion.payload.action === 'update') return '更新这条待办？'
  if (suggestion.payload.action === 'delete') return '删除这条待办？'
  return '记进待办？'
}

import { useEffect, useState } from 'react'
import {
  ApiError,
  type Suggestion,
  acceptSuggestion,
  dismissSuggestion,
} from '@/api/client'
import { StarIcon } from '@/components/icons'
import { formatRoute } from '@/router'
import styles from './InlineSuggestions.module.css'

interface InlineSuggestionsProps {
  suggestions: Suggestion[]
  /** 私聊来源：采纳后建议内容对所有伙伴可见，需在卡片底部轻提示。 */
  fromPrivateChat?: boolean
}

export function InlineSuggestions({ suggestions, fromPrivateChat }: InlineSuggestionsProps) {
  const [pending, setPending] = useState(suggestions)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [blocked, setBlocked] = useState<Record<string, 'no_writable_account' | 'suggestion_expired'>>({})
  const suggestionKey = suggestions.map((item) => item.id).join('|')

  useEffect(() => {
    setPending(suggestions)
    setError(null)
    setBlocked({})
  }, [suggestionKey])

  if (pending.length === 0) return null

  const decide = async (
    suggestion: Suggestion,
    action: 'accept' | 'dismiss',
    opts?: { fallbackLocal?: boolean },
  ) => {
    setBusyId(suggestion.id)
    setError(null)
    try {
      if (action === 'accept') {
        await acceptSuggestion(suggestion.id, opts)
      } else {
        await dismissSuggestion(suggestion.id)
      }
      setPending((current) => current.filter((item) => item.id !== suggestion.id))
    } catch (err) {
      const suggestionCode = err instanceof ApiError ? err.code : null
      if (
        action === 'accept'
        && suggestion.kind === 'schedule'
        && (suggestionCode === 'no_writable_account' || suggestionCode === 'suggestion_expired')
      ) {
        setBlocked((current) => ({ ...current, [suggestion.id]: suggestionCode }))
        return
      }
      setError(err instanceof Error ? err.message : '处理失败')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className={styles.stack}>
      {pending.map((suggestion) => {
        const busy = busyId === suggestion.id
        const block = blocked[suggestion.id]
        const expired = block === 'suggestion_expired'
        const noAccount = block === 'no_writable_account'
        return (
          <div key={suggestion.id} className={styles.card}>
            <span className={styles.icon} aria-hidden>
              <StarIcon width={15} height={15} />
            </span>
            <div className={styles.body}>
              <span className={styles.kicker}>
                {expired ? '时间已过' : suggestion.kind === 'schedule' ? '排进日程？' : '记进目标？'}
              </span>
              <p className={styles.title}>{suggestionTitle(suggestion)}</p>
              {suggestion.kind === 'schedule' && (
                <span className={styles.meta}>{scheduleTimeLabel(suggestion)}</span>
              )}
            </div>
            <div className={styles.actions}>
              {noAccount ? (
                <>
                  <button
                    className={styles.accept}
                    disabled={busy}
                    onClick={() => {
                      window.location.hash = formatRoute({ kind: 'settings', tab: 'schedule' })
                    }}
                  >
                    连接 Microsoft 日历
                  </button>
                  <button
                    className={styles.localFallback}
                    disabled={busy}
                    onClick={() => void decide(suggestion, 'accept', { fallbackLocal: true })}
                  >
                    先存在本地
                  </button>
                </>
              ) : (
                <>
                  {!expired && (
                    <button
                      className={styles.accept}
                      disabled={busy}
                      onClick={() => void decide(suggestion, 'accept')}
                    >
                      采纳
                    </button>
                  )}
                  <button
                    className={styles.dismiss}
                    disabled={busy}
                    onClick={() => void decide(suggestion, 'dismiss')}
                  >
                    忽略
                  </button>
                </>
              )}
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
  const value = suggestion.kind === 'schedule' ? suggestion.payload.subject : suggestion.payload.title
  return value.trim() || '未命名建议'
}

function scheduleTimeLabel(suggestion: Extract<Suggestion, { kind: 'schedule' }>): string {
  const [, month, day] = suggestion.payload.date.split('-').map(Number)
  const dateLabel = Number.isFinite(month) && Number.isFinite(day)
    ? `${month}月${day}日`
    : suggestion.payload.date
  if (suggestion.payload.all_day) return `${dateLabel} 全天`
  const start = suggestion.payload.start_time ?? '09:00'
  const end = suggestion.payload.end_time ?? addOneHour(start)
  return `${dateLabel} ${start}–${end}`
}

function addOneHour(value: string): string {
  const [hour, minute] = value.split(':').map(Number)
  if (hour === undefined || minute === undefined || !Number.isFinite(hour) || !Number.isFinite(minute)) return value
  const total = (hour * 60 + minute + 60) % (24 * 60)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}

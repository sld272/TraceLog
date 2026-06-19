import { useEffect, useState } from 'react'
import {
  type Suggestion,
  acceptSuggestion,
  dismissSuggestion,
} from '@/api/client'
import styles from './InlineSuggestions.module.css'

interface InlineSuggestionsProps {
  suggestions: Suggestion[]
}

export function InlineSuggestions({ suggestions }: InlineSuggestionsProps) {
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
    <div className={styles.panel}>
      {pending.map((suggestion) => (
        <div key={suggestion.id} className={styles.item}>
          <span className={styles.label}>{suggestion.kind === 'goal' ? '目标建议' : '待办建议'}</span>
          <p className={styles.title}>{suggestionTitle(suggestion)}</p>
          <p className={styles.question}>
            {suggestionQuestion(suggestion)}
          </p>
          <div className={styles.actions}>
            <button
              className={styles.accept}
              disabled={busyId === suggestion.id}
              onClick={() => void decide(suggestion, 'accept')}
            >
              采纳
            </button>
            <button
              className={styles.dismiss}
              disabled={busyId === suggestion.id}
              onClick={() => void decide(suggestion, 'dismiss')}
            >
              忽略
            </button>
          </div>
        </div>
      ))}
      {error && <p className={styles.error}>{error}</p>}
    </div>
  )
}

function suggestionTitle(suggestion: Suggestion): string {
  const value = suggestion.kind === 'goal' ? suggestion.payload.title : suggestion.payload.task
  return typeof value === 'string' && value.trim() ? value : '未命名建议'
}

function suggestionQuestion(suggestion: Suggestion): string {
  if (suggestion.kind === 'goal') return '要正式记进目标吗？'
  if (suggestion.payload.action === 'update') return '要更新这条待办吗？'
  if (suggestion.payload.action === 'delete') return '要删除这条待办吗？'
  return '要记进待办吗？'
}

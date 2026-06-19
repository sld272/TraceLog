import { useCallback, useEffect, useState } from 'react'
import {
  type Suggestion,
  acceptSuggestion,
  dismissSuggestion,
  listSuggestions,
} from '@/api/client'
import styles from '@/pages/WorkspacePages.module.css'

interface SuggestionTrayProps {
  onAccepted?: (kind: Suggestion['kind']) => void
}

export function SuggestionTray({ onAccepted }: SuggestionTrayProps) {
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setLoading(true)
      setSuggestions(await listSuggestions())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '建议加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const accept = async (suggestion: Suggestion) => {
    setBusyId(suggestion.id)
    setError(null)
    try {
      await acceptSuggestion(suggestion.id)
      setSuggestions((current) => current.filter((item) => item.id !== suggestion.id))
      onAccepted?.(suggestion.kind)
    } catch (err) {
      setError(err instanceof Error ? err.message : '采纳失败')
    } finally {
      setBusyId(null)
    }
  }

  const dismiss = async (suggestion: Suggestion) => {
    setBusyId(suggestion.id)
    setError(null)
    try {
      await dismissSuggestion(suggestion.id)
      setSuggestions((current) => current.filter((item) => item.id !== suggestion.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : '忽略失败')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className={styles.suggestionTray}>
      <div className={styles.suggestionHeader}>
        <div>
          <h2>建议托盘</h2>
          <p>系统只提议；采纳后才会进入你的目标或待办。</p>
        </div>
        <button className={styles.ghostButton} onClick={() => void refresh()} disabled={loading}>
          刷新
        </button>
      </div>
      {error && <p className={styles.inlineWorkbenchError}>{error}</p>}
      {loading ? (
        <p className={styles.suggestionEmpty}>加载中...</p>
      ) : suggestions.length === 0 ? (
        <p className={styles.suggestionEmpty}>暂无待确认建议</p>
      ) : (
        <div className={styles.suggestionList}>
          {suggestions.map((suggestion) => (
            <article key={suggestion.id} className={styles.suggestionItem}>
              <div className={styles.suggestionBody}>
                <span className={styles.pill}>{suggestion.kind === 'goal' ? '目标' : '待办'}</span>
                <h3>{suggestionTitle(suggestion)}</h3>
                <p>{suggestionMeta(suggestion)}</p>
              </div>
              <div className={styles.suggestionActions}>
                <button
                  className={styles.ghostButton}
                  disabled={busyId === suggestion.id}
                  onClick={() => void dismiss(suggestion)}
                >
                  忽略
                </button>
                <button
                  className={styles.button}
                  disabled={busyId === suggestion.id}
                  onClick={() => void accept(suggestion)}
                >
                  {busyId === suggestion.id ? '处理中...' : '采纳'}
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  )
}

function suggestionTitle(suggestion: Suggestion): string {
  const value = suggestion.kind === 'goal' ? suggestion.payload.title : suggestion.payload.task
  return typeof value === 'string' && value.trim() ? value : '未命名建议'
}

function suggestionMeta(suggestion: Suggestion): string {
  const parts = [`置信度 ${Math.round(suggestion.confidence * 100)}%`]
  if (suggestion.kind === 'goal') {
    parts.push(suggestion.payload.horizon === 'short' ? '短期' : '长期')
  } else {
    if (suggestion.payload.action === 'update') parts.push('更新待办')
    if (suggestion.payload.action === 'delete') parts.push('删除待办')
    if (suggestion.payload.action === 'create') parts.push('新增待办')
    if (typeof suggestion.payload.date === 'string' && suggestion.payload.date) {
      parts.push(suggestion.payload.date)
    }
  }
  if (suggestion.evidence_ref) parts.push(`来源 ${suggestion.evidence_ref}`)
  return parts.join(' · ')
}

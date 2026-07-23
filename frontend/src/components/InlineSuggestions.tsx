import { useEffect, useState } from 'react'
import {
  ApiError,
  type GoalHorizon,
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

/** Inline edit form state — only one card edits at a time. */
type GoalDraft = {
  id: string
  kind: 'goal'
  title: string
  detail: string
  horizon: GoalHorizon
}

type ScheduleDraft = {
  id: string
  kind: 'schedule'
  subject: string
  date: string
  startTime: string
  endTime: string
  allDay: boolean
}

type EditDraft = GoalDraft | ScheduleDraft

function draftFromSuggestion(suggestion: Suggestion): EditDraft {
  if (suggestion.kind === 'goal') {
    return {
      id: suggestion.id,
      kind: 'goal',
      title: suggestion.payload.title,
      detail: suggestion.payload.detail ?? '',
      horizon: suggestion.payload.horizon,
    }
  }
  return {
    id: suggestion.id,
    kind: 'schedule',
    subject: suggestion.payload.subject,
    date: suggestion.payload.date,
    startTime: suggestion.payload.start_time ?? '',
    endTime: suggestion.payload.end_time ?? '',
    allDay: suggestion.payload.all_day,
  }
}

function overridesFromDraft(draft: EditDraft): Record<string, unknown> {
  if (draft.kind === 'goal') {
    return { title: draft.title, detail: draft.detail, horizon: draft.horizon }
  }
  return {
    subject: draft.subject,
    date: draft.date,
    all_day: draft.allDay,
    start_time: draft.allDay ? null : draft.startTime || null,
    end_time: draft.allDay ? null : draft.endTime || null,
  }
}

export function InlineSuggestions({ suggestions, fromPrivateChat }: InlineSuggestionsProps) {
  const [pending, setPending] = useState(suggestions)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [blocked, setBlocked] = useState<Record<string, 'no_writable_account' | 'suggestion_expired'>>({})
  const [editing, setEditing] = useState<EditDraft | null>(null)
  // Overrides remembered when an edit-save falls back to the local-account choice.
  const [savedOverrides, setSavedOverrides] = useState<Record<string, Record<string, unknown>>>({})
  const suggestionKey = suggestions.map((item) => item.id).join('|')

  useEffect(() => {
    setPending(suggestions)
    setError(null)
    setBlocked({})
    setEditing(null)
    setSavedOverrides({})
  }, [suggestionKey])

  if (pending.length === 0) return null

  const drop = (id: string) => setPending((current) => current.filter((item) => item.id !== id))

  const decide = async (
    suggestion: Suggestion,
    action: 'accept' | 'dismiss',
    opts?: { fallbackLocal?: boolean; overrides?: Record<string, unknown> },
  ) => {
    setBusyId(suggestion.id)
    setError(null)
    try {
      if (action === 'accept') {
        await acceptSuggestion(suggestion.id, opts)
      } else {
        await dismissSuggestion(suggestion.id)
      }
      drop(suggestion.id)
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

  const startEdit = (suggestion: Suggestion) => {
    setError(null)
    setEditing(draftFromSuggestion(suggestion))
  }

  const saveEdit = async () => {
    if (editing === null) return
    const suggestion = pending.find((item) => item.id === editing.id)
    if (!suggestion) return
    const overrides = overridesFromDraft(editing)
    setBusyId(editing.id)
    setError(null)
    try {
      await acceptSuggestion(editing.id, { overrides })
      drop(editing.id)
      setEditing(null)
    } catch (err) {
      const suggestionCode = err instanceof ApiError ? err.code : null
      if (suggestion.kind === 'schedule' && suggestionCode === 'no_writable_account') {
        // Preserve the edits so the local-account fallback saves the new time.
        setSavedOverrides((current) => ({ ...current, [editing.id]: overrides }))
        setBlocked((current) => ({ ...current, [editing.id]: 'no_writable_account' }))
        setEditing(null)
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
        const isEditing = editing?.id === suggestion.id

        if (isEditing && editing) {
          return (
            <div key={suggestion.id} className={`${styles.card} ${styles.cardEditing}`}>
              <SuggestionEditForm
                draft={editing}
                busy={busy}
                onChange={setEditing}
                onSave={() => void saveEdit()}
                onCancel={() => setEditing(null)}
              />
            </div>
          )
        }

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
              {expired && <span className={styles.hint}>改时间后可采纳</span>}
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
                    onClick={() =>
                      void decide(suggestion, 'accept', {
                        fallbackLocal: true,
                        overrides: savedOverrides[suggestion.id],
                      })
                    }
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
                    className={styles.edit}
                    disabled={busy}
                    onClick={() => startEdit(suggestion)}
                  >
                    编辑
                  </button>
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

function SuggestionEditForm({
  draft,
  busy,
  onChange,
  onSave,
  onCancel,
}: {
  draft: EditDraft
  busy: boolean
  onChange: (draft: EditDraft) => void
  onSave: () => void
  onCancel: () => void
}) {
  return (
    <form
      className={styles.form}
      onSubmit={(event) => {
        event.preventDefault()
        onSave()
      }}
    >
      {draft.kind === 'goal' ? (
        <>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>标题</span>
            <input
              className={styles.input}
              value={draft.title}
              onChange={(event) => onChange({ ...draft, title: event.target.value })}
              autoFocus
            />
          </label>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>详情</span>
            <textarea
              className={styles.textarea}
              value={draft.detail}
              rows={2}
              onChange={(event) => onChange({ ...draft, detail: event.target.value })}
            />
          </label>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>期限</span>
            <select
              className={styles.input}
              value={draft.horizon}
              onChange={(event) => onChange({ ...draft, horizon: event.target.value as GoalHorizon })}
            >
              <option value="short">近期</option>
              <option value="long">长期</option>
            </select>
          </label>
        </>
      ) : (
        <>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>主题</span>
            <input
              className={styles.input}
              value={draft.subject}
              onChange={(event) => onChange({ ...draft, subject: event.target.value })}
              autoFocus
            />
          </label>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>日期</span>
            <input
              type="date"
              className={styles.input}
              value={draft.date}
              onChange={(event) => onChange({ ...draft, date: event.target.value })}
            />
          </label>
          {!draft.allDay && (
            <div className={styles.timeRow}>
              <label className={styles.field}>
                <span className={styles.fieldLabel}>开始</span>
                <input
                  type="time"
                  className={styles.input}
                  value={draft.startTime}
                  onChange={(event) => onChange({ ...draft, startTime: event.target.value })}
                />
              </label>
              <label className={styles.field}>
                <span className={styles.fieldLabel}>结束</span>
                <input
                  type="time"
                  className={styles.input}
                  value={draft.endTime}
                  onChange={(event) => onChange({ ...draft, endTime: event.target.value })}
                />
              </label>
            </div>
          )}
          <label className={styles.checkboxRow}>
            <input
              type="checkbox"
              checked={draft.allDay}
              onChange={(event) => onChange({ ...draft, allDay: event.target.checked })}
            />
            <span>全天</span>
          </label>
        </>
      )}
      <div className={styles.formActions}>
        <button type="submit" className={styles.save} disabled={busy}>
          保存并采纳
        </button>
        <button type="button" className={styles.cancel} disabled={busy} onClick={onCancel}>
          取消
        </button>
      </div>
    </form>
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

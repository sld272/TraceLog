import { useEffect, useState } from 'react'
import {
  type MigrationConflict,
  type MigrationPreview,
  type MigrationResult,
  ApiError,
  dismissMigrationPrompt,
  migrateLocalEvents,
  previewLocalMigration,
} from '@/api/client'
import { monthDayLabel } from '@/utils/schedule'
import workspaceStyles from '@/pages/WorkspacePages.module.css'
import styles from './ScheduleMigrationDialog.module.css'

type Decision = 'skip' | 'create'
type Phase = 'intro' | 'previewing' | 'conflicts' | 'migrating' | 'result'

interface ScheduleMigrationDialogProps {
  /** 入口来源：'prompt'（自动弹）暂不时抑制后端 flag；'settings' 只关闭。 */
  source: 'prompt' | 'settings'
  /** 待迁移的本地日程条数（用于引导页文案）。 */
  localEventCount: number
  /** 纯关闭（不刷新数据）：用于「暂不」。 */
  onClose: () => void
  /** 迁移完成 / 终态退出：调用方据此 invalidate 缓存并刷新数据。 */
  onFinished: () => void
}

/** 本地→Outlook 迁移引导对话框（引导 → 预检 → 冲突解决 → 迁移中 → 结果）。 */
export function ScheduleMigrationDialog({
  source,
  localEventCount,
  onClose,
  onFinished,
}: ScheduleMigrationDialogProps) {
  const [phase, setPhase] = useState<Phase>('intro')
  const [busy, setBusy] = useState(false)
  const [conflicts, setConflicts] = useState<MigrationConflict[]>([])
  const [decisions, setDecisions] = useState<Record<string, Decision>>({})
  const [result, setResult] = useState<MigrationResult | null>(null)
  /** 终态错误（409「状态已变」或意外失败）。canRetry 时提供「重试」。 */
  const [terminalError, setTerminalError] = useState<{ message: string; canRetry: boolean } | null>(
    null,
  )

  /* 迁移已发生（结果页/终态错误）后，任何关闭路径都必须走 onFinished 刷新，
   * 否则页面会残留已被迁走的本地日程幻影。 */
  const dismissiveClose = () => {
    if (busy) return
    if (phase === 'result' || terminalError !== null) onFinished()
    else onClose()
  }

  useEffect(() => {
    const handleKey = (keyEvent: KeyboardEvent) => {
      if (keyEvent.key === 'Escape') dismissiveClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, phase, terminalError, onClose, onFinished])

  /** 「暂不」：prompt 入口写抑制 flag，settings 入口只关闭。 */
  const handleDismiss = async () => {
    if (source === 'prompt') {
      setBusy(true)
      try {
        await dismissMigrationPrompt()
      } catch {
        /* 抑制失败不阻断关闭：下次仍可能再弹，无害 */
      } finally {
        setBusy(false)
      }
    }
    onClose()
  }

  /** 直接执行迁移（无冲突或已收集完冲突决策）。 */
  const runMigration = async (finalDecisions: Record<string, Decision>) => {
    setPhase('migrating')
    setBusy(true)
    try {
      const migrationResult = await migrateLocalEvents(finalDecisions)
      setResult(migrationResult)
      setPhase('result')
    } catch (err) {
      handleFailure(err)
    } finally {
      setBusy(false)
    }
  }

  /** 预检：拉冲突。无冲突直接迁移，有冲突进入解决列表。 */
  const runPreview = async () => {
    setPhase('previewing')
    setBusy(true)
    setTerminalError(null)
    try {
      const preview: MigrationPreview = await previewLocalMigration()
      if (preview.conflicts.length === 0) {
        await runMigration({})
        return
      }
      setConflicts(preview.conflicts)
      setDecisions(
        Object.fromEntries(preview.conflicts.map((conflict) => [conflict.local.id, 'skip'])),
      )
      setPhase('conflicts')
    } catch (err) {
      handleFailure(err)
    } finally {
      setBusy(false)
    }
  }

  /** 409 视为状态已变（如另一端已删本地账号）；其它错误可重试。 */
  const handleFailure = (err: unknown) => {
    if (err instanceof ApiError && err.status === 409) {
      setTerminalError({
        message:
          typeof err.message === 'string' && err.message
            ? `${err.message}（本地日历状态可能已在其它设备变更）`
            : '本地日历状态已变化，请关闭后刷新。',
        canRetry: false,
      })
    } else {
      setTerminalError({
        message: err instanceof Error ? err.message : '迁移失败，请重试。',
        canRetry: true,
      })
    }
  }

  const finish = () => onFinished()

  const renderBody = () => {
    if (terminalError) {
      return (
        <div className={styles.body}>
          <div className={styles.resultStack}>
            <div className={styles.errorBox}>{terminalError.message}</div>
          </div>
        </div>
      )
    }
    switch (phase) {
      case 'intro':
        return (
          <div className={styles.body}>
            <p className={styles.resultMeta}>
              共 <span className={styles.strong}>{localEventCount}</span>{' '}
              条本地日程将写入你的 Outlook 日历；
              <span className={styles.strong}>迁移完成后本地日历将被移除</span>。
              已绑定的目标会一并转移。
            </p>
          </div>
        )
      case 'previewing':
        return (
          <div className={styles.center}>
            <span className={styles.spinner} aria-hidden="true" />
            <span>正在检查是否有重复日程…</span>
          </div>
        )
      case 'conflicts':
        return (
          <div className={styles.body}>
            <p className={styles.conflictIntro}>
              以下本地日程在 Outlook 中疑似已存在。默认「跳过」（不重复创建，仅把目标绑定转移到现有事件）；
              如确为不同日程，选「仍然创建」。
            </p>
            <div className={styles.conflictList}>
              {conflicts.map((conflict) => {
                const decision = decisions[conflict.local.id] ?? 'skip'
                return (
                  <div key={conflict.local.id} className={styles.conflictRow}>
                    <div className={styles.compare}>
                      <div className={styles.side}>
                        <span className={styles.sideLabel}>本地</span>
                        <span className={styles.eventTitle}>{conflict.local.subject}</span>
                        <span className={styles.eventTime}>{formatRefTime(conflict.local)}</span>
                      </div>
                      <span className={styles.vs} aria-hidden="true">≈</span>
                      <div className={styles.side}>
                        <span className={styles.sideLabel}>Outlook 已有</span>
                        <span className={styles.eventTitle}>{conflict.existing.subject}</span>
                        <span className={styles.eventTime}>{formatRefTime(conflict.existing)}</span>
                      </div>
                    </div>
                    <div className={styles.radioGroup}>
                      <label className={styles.radioOption}>
                        <input
                          type="radio"
                          name={`decision-${conflict.local.id}`}
                          checked={decision === 'skip'}
                          onChange={() => setDecision(conflict.local.id, 'skip')}
                        />
                        跳过（已存在）
                      </label>
                      <label className={styles.radioOption}>
                        <input
                          type="radio"
                          name={`decision-${conflict.local.id}`}
                          checked={decision === 'create'}
                          onChange={() => setDecision(conflict.local.id, 'create')}
                        />
                        仍然创建
                      </label>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )
      case 'migrating':
        return (
          <div className={styles.center}>
            <span className={styles.spinner} aria-hidden="true" />
            <span>正在迁移日程到 Outlook…</span>
          </div>
        )
      case 'result':
        return renderResult()
      default:
        return null
    }
  }

  const renderResult = () => {
    if (!result) return null
    if (result.status === 'ok') {
      return (
        <div className={styles.body}>
          <div className={styles.resultStack}>
            <p className={styles.resultLine}>
              已迁入 <span className={styles.strong}>{result.migrated}</span> 条
              {result.skipped > 0 ? `、跳过 ${result.skipped} 条（目标绑定已转移）` : ''}。
            </p>
            <p className={styles.resultMeta}>本地日历已移除，之后的日程将直接写入 Outlook。</p>
          </div>
        </div>
      )
    }
    return (
      <div className={styles.body}>
        <div className={styles.resultStack}>
          <p className={styles.resultLine}>
            已迁入 <span className={styles.strong}>{result.migrated}</span> 条
            {result.skipped > 0 ? `、跳过 ${result.skipped} 条` : ''}，
            还剩 <span className={styles.strong}>{result.remaining}</span> 条未处理。
          </p>
          {result.error && <div className={styles.errorBox}>{result.error}</div>}
          <p className={styles.resultMeta}>本地日历仍保留剩余日程，可重试继续迁移。</p>
        </div>
      </div>
    )
  }

  const setDecision = (localId: string, decision: Decision) =>
    setDecisions((prev) => ({ ...prev, [localId]: decision }))

  const renderActions = () => {
    if (terminalError) {
      return (
        <>
          {terminalError.canRetry && (
            <button
              className={workspaceStyles.ghostButton}
              type="button"
              onClick={() => void runPreview()}
            >
              重试
            </button>
          )}
          <button className={workspaceStyles.button} type="button" onClick={finish}>
            关闭
          </button>
        </>
      )
    }
    switch (phase) {
      case 'intro':
        return (
          <>
            <button
              className={workspaceStyles.ghostButton}
              type="button"
              onClick={() => void handleDismiss()}
              disabled={busy}
            >
              暂不
            </button>
            <button
              className={workspaceStyles.button}
              type="button"
              onClick={() => void runPreview()}
              disabled={busy}
            >
              开始迁移
            </button>
          </>
        )
      case 'conflicts':
        return (
          <>
            <button
              className={workspaceStyles.ghostButton}
              type="button"
              onClick={onClose}
              disabled={busy}
            >
              取消
            </button>
            <button
              className={workspaceStyles.button}
              type="button"
              onClick={() => void runMigration(decisions)}
              disabled={busy}
            >
              确认迁移
            </button>
          </>
        )
      case 'result':
        if (result?.status === 'partial') {
          return (
            <>
              <button className={workspaceStyles.ghostButton} type="button" onClick={finish}>
                关闭
              </button>
              <button
                className={workspaceStyles.button}
                type="button"
                onClick={() => void runPreview()}
              >
                重试
              </button>
            </>
          )
        }
        return (
          <button className={workspaceStyles.button} type="button" onClick={finish}>
            完成
          </button>
        )
      default:
        return null
    }
  }

  const showActions = terminalError !== null || (phase !== 'previewing' && phase !== 'migrating')

  return (
    <div className={styles.overlay} onClick={dismissiveClose}>
      <div
        className={styles.dialog}
        onClick={(clickEvent) => clickEvent.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="把本地日程迁入 Outlook"
      >
        <div className={styles.header}>
          <h2>把本地日程迁入 Outlook</h2>
        </div>
        {renderBody()}
        {showActions && <div className={styles.actions}>{renderActions()}</div>}
      </div>
    </div>
  )
}

/** 冲突对照里的时间：全天 → 'M月D日 全天'，否则 'M月D日 HH:MM'。 */
function formatRefTime(ref: { start_local: string; all_day: boolean }): string {
  const key = String(ref.start_local).slice(0, 10)
  const dateLabel = monthDayLabel(key)
  if (ref.all_day) return `${dateLabel} 全天`
  return `${dateLabel} ${String(ref.start_local).slice(11, 16)}`
}

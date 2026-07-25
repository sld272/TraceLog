import { useEffect, useMemo, useRef, useState } from 'react'
import {
  type Goal,
  type GoalActivity,
  type GoalActivityKind,
  listGoals,
  recordGoalActivity,
} from '@/api/client'
import { LoadingDots } from '@/components/icons'
import { ACTIVITY_KIND_LABELS, MANUAL_ACTIVITY_KINDS } from '@/utils/goalActivity'
import styles from './GoalActivityTagger.module.css'

interface GoalActivityTaggerProps {
  /** 要补标的证据，形如 `post:{post_id}`。 */
  evidenceRef: string
  /** 该证据已有 active 动态的 goal_id → kind；用于禁用重复提交。 */
  taggedKindByGoal: Map<string, GoalActivityKind>
  onClose: () => void
  /** 服务端确认写入了一条 active 动态（goalTitle 用于就地渲染 chip）。 */
  onRecorded: (activity: GoalActivity, goalTitle: string) => void
}

type Notice = { tone: 'blocked' | 'info' | 'error'; text: string }

function errorText(err: unknown): string {
  const message = err instanceof Error ? err.message.trim() : ''
  return message || '请稍后重试'
}

export function GoalActivityTagger({
  evidenceRef,
  taggedKindByGoal,
  onClose,
  onRecorded,
}: GoalActivityTaggerProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [goals, setGoals] = useState<Goal[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [picked, setPicked] = useState<Goal | null>(null)
  const [submitting, setSubmitting] = useState<GoalActivityKind | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  /* 撞上墓碑的目标：换个 kind 再提交也是同样结果，别让用户在这里绕圈。 */
  const [tombstoned, setTombstoned] = useState<ReadonlySet<string>>(new Set())

  /* 目标现拉现用：补标是低频动作，且页面加载后可能又采纳了新目标。 */
  useEffect(() => {
    let cancelled = false
    listGoals({ status: 'active' })
      .then((data) => {
        if (!cancelled) setGoals(data)
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(`目标加载失败：${errorText(err)}`)
      })
    return () => {
      cancelled = true
    }
  }, [])

  /* 外点关闭（渲染后再挂 mousedown，避开触发这次打开的那一下点击）。 */
  useEffect(() => {
    const handleDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handleDown)
    return () => document.removeEventListener('mousedown', handleDown)
  }, [onClose])

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  /* focus 的目标排前面：当前关注的通常就是要补标的那个。 */
  const ordered = useMemo(() => {
    if (!goals) return []
    return [...goals.filter((goal) => goal.focus), ...goals.filter((goal) => !goal.focus)]
  }, [goals])

  const submit = async (goal: Goal, kind: GoalActivityKind) => {
    setSubmitting(kind)
    setNotice(null)
    try {
      /* 不传 evidence_span：用户亲手指定了这条记录，引文没有解释价值。 */
      const activity = await recordGoalActivity(goal.id, { kind, evidence_ref: evidenceRef })
      /* 幂等墓碑：这条（记录, 目标）曾被撤销过，后端原样返回那条 rejected 记录，
         既不复活也不新建。必须说清楚，否则用户只看到「提交成功但 chip 没出现」。 */
      if (activity.status === 'rejected') {
        setPicked(null)
        setTombstoned((current) => new Set(current).add(goal.id))
        setNotice({
          tone: 'blocked',
          text: `没有新增。这条记录在「${goal.title}」下被撤销过，撤销会一直保留（自动识别靠它校准），同一条记录对同一个目标不会再标第二次。`,
        })
        return
      }
      onRecorded(activity, goal.title)
      /* 同一（记录, 目标）只存一条：若期间自动检测已写入别的 kind，返回的是那条。 */
      if (activity.kind !== kind) {
        setPicked(null)
        setNotice({
          tone: 'info',
          text: `「${goal.title}」下已经有一条「${ACTIVITY_KIND_LABELS[activity.kind]}」了。同一条记录对同一个目标只留一条，已沿用原有的。`,
        })
        return
      }
      onClose()
    } catch (err) {
      setNotice({ tone: 'error', text: `标记失败：${errorText(err)}` })
    } finally {
      setSubmitting(null)
    }
  }

  return (
    <div ref={ref} className={styles.popover} role="dialog" aria-label="标记为目标动态">
      <div className={styles.head}>
        {picked && (
          <button
            type="button"
            className={styles.back}
            onClick={() => setPicked(null)}
            disabled={submitting !== null}
            aria-label="返回目标列表"
          >
            ‹
          </button>
        )}
        <span className={styles.headTitle}>{picked ? picked.title : '标记为目标动态'}</span>
      </div>

      {notice && <p className={styles[notice.tone]} role="status">{notice.text}</p>}

      {picked ? (
        <>
          <p className={styles.hint}>这条记录对该目标算什么？</p>
          <div className={styles.kinds}>
            {MANUAL_ACTIVITY_KINDS.map((kind) => (
              <button
                key={kind}
                type="button"
                className={styles.kind}
                onClick={() => void submit(picked, kind)}
                disabled={submitting !== null}
              >
                {submitting === kind ? <LoadingDots /> : ACTIVITY_KIND_LABELS[kind]}
              </button>
            ))}
          </div>
        </>
      ) : loadError ? (
        <p className={styles.error}>{loadError}</p>
      ) : goals === null ? (
        <p className={styles.empty}>加载中...</p>
      ) : ordered.length === 0 ? (
        <p className={styles.empty}>还没有进行中的目标。</p>
      ) : (
        <ul className={styles.goals}>
          {ordered.map((goal) => {
            const tagged = taggedKindByGoal.get(goal.id)
            const blocked = tombstoned.has(goal.id)
            return (
              <li key={goal.id}>
                <button
                  type="button"
                  className={styles.goal}
                  onClick={() => {
                    setNotice(null)
                    setPicked(goal)
                  }}
                  disabled={tagged !== undefined || blocked}
                  title={
                    tagged !== undefined
                      ? '这条记录已经标过这个目标了'
                      : blocked
                        ? '撤销过的记录不会重新标记'
                        : undefined
                  }
                >
                  <span className={styles.goalTitle}>{goal.title}</span>
                  {tagged !== undefined ? (
                    <span className={styles.goalTagged}>已标 · {ACTIVITY_KIND_LABELS[tagged]}</span>
                  ) : blocked ? (
                    <span className={styles.goalTagged}>已撤销过</span>
                  ) : null}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

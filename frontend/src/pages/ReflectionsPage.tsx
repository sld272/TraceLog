import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type Job,
  type MemoryRevisionSummary,
  type ReflectionScope,
  type SoulReflectionScope,
  getJob,
  listProfileRevisions,
  listSoulMemoryRevisions,
  listSouls,
  previewGlobalReflection,
  previewSoulReflections,
  retryJob,
  cancelJob,
  triggerGlobalReflection,
  triggerSoulReflections,
} from '@/api/client'
import {
  formatAbsoluteTime,
  formatDate,
  formatDateTimeAttribute,
  formatSmartTime,
} from '@/utils/date'
import { PollTimeoutError, pollUntil } from '@/utils/polling'
import styles from './WorkspacePages.module.css'

const RECENT_REVISION_LIMIT = 8
const REFLECTION_POLL_INTERVAL_MS = 3000
const REFLECTION_POLL_TIMEOUT_MS = 30000
const TERMINAL_JOB_STATUSES = new Set<Job['status']>(['succeeded', 'failed', 'cancelled'])

interface ReflectionsPageProps {
  onReflectionSettled?: () => void
}

export function ReflectionsPage({ onReflectionSettled }: ReflectionsPageProps) {
  const [globalScope, setGlobalScope] = useState<ReflectionScope | null>(null)
  const [soulScopes, setSoulScopes] = useState<SoulReflectionScope[]>([])
  const [recentRevisions, setRecentRevisions] = useState<MemoryRevisionSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState<'global' | 'souls' | null>(null)
  const [activeJob, setActiveJob] = useState<Job | null>(null)
  const [lastFailedJob, setLastFailedJob] = useState<Job | null>(null)
  const [jobActionBusy, setJobActionBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [revisionError, setRevisionError] = useState<string | null>(null)
  const pollAbortRef = useRef<AbortController | null>(null)

  const fetchPreview = useCallback(async () => {
    try {
      const [globalData, soulData] = await Promise.all([
        previewGlobalReflection(),
        previewSoulReflections(),
      ])
      setGlobalScope(globalData)
      setSoulScopes(soulData)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    }
  }, [])

  const fetchRecentRevisions = useCallback(async () => {
    try {
      const [profileRevisions, souls] = await Promise.all([
        listProfileRevisions(10),
        listSouls(false),
      ])
      const soulRevisionGroups = await Promise.all(
        souls.map((soul) => listSoulMemoryRevisions(soul.name, 5)),
      )
      const revisions = [...profileRevisions, ...soulRevisionGroups.flat()]
        .filter(isRevisionOutput)
        .sort((a, b) => {
          const timeDelta = b.created_at - a.created_at
          return timeDelta === 0 ? b.id - a.id : timeDelta
        })
        .slice(0, RECENT_REVISION_LIMIT)
      setRecentRevisions(revisions)
      setRevisionError(null)
    } catch {
      setRecentRevisions([])
      setRevisionError('整理记录加载失败')
    }
  }, [])

  const refreshPage = useCallback(async () => {
    setLoading(true)
    try {
      await Promise.all([
        fetchPreview(),
        fetchRecentRevisions(),
      ])
    } finally {
      setLoading(false)
    }
  }, [fetchPreview, fetchRecentRevisions])

  useEffect(() => {
    refreshPage()
  }, [refreshPage])

  useEffect(() => {
    return () => {
      pollAbortRef.current?.abort()
    }
  }, [])

  const runGlobal = async () => {
    setRunning('global')
    setError(null)
    pollAbortRef.current?.abort()
    const controller = new AbortController()
    pollAbortRef.current = controller
    try {
      const result = await triggerGlobalReflection()
      setNotice(`全局整理已加入队列：#${result.job_id}`)
      setLastFailedJob(null)
      await waitForReflectionJob(result.job_id, controller.signal)
    } catch (err) {
      handleReflectionRunError(err, '全局整理')
    } finally {
      if (pollAbortRef.current === controller) pollAbortRef.current = null
      setRunning(null)
    }
  }

  const runSouls = async () => {
    setRunning('souls')
    setError(null)
    pollAbortRef.current?.abort()
    const controller = new AbortController()
    pollAbortRef.current = controller
    try {
      const result = await triggerSoulReflections()
      setNotice(`人格记忆整理已加入队列：#${result.job_id}`)
      setLastFailedJob(null)
      await waitForReflectionJob(result.job_id, controller.signal)
    } catch (err) {
      handleReflectionRunError(err, '人格记忆整理')
    } finally {
      if (pollAbortRef.current === controller) pollAbortRef.current = null
      setRunning(null)
    }
  }

  const waitForReflectionJob = async (jobId: number, signal: AbortSignal) => {
    try {
      const job = await pollUntil({
        intervalMs: REFLECTION_POLL_INTERVAL_MS,
        timeoutMs: REFLECTION_POLL_TIMEOUT_MS,
        signal,
        tick: async () => {
          const [job] = await Promise.all([
            getJob(jobId),
            fetchPreview(),
          ])
          setActiveJob(job)
          return job
        },
        isDone: (job) => TERMINAL_JOB_STATUSES.has(job.status),
      })

      await Promise.all([
        fetchPreview(),
        fetchRecentRevisions(),
      ])
      onReflectionSettled?.()

      if (job.status === 'succeeded') {
        setActiveJob(null)
        setLastFailedJob(null)
        setNotice(`整理已完成：#${job.id}`)
      } else if (job.status === 'cancelled') {
        setActiveJob(null)
        setNotice(`整理已取消：#${job.id}`)
      } else {
        setActiveJob(null)
        setLastFailedJob(job)
        setError(job.error ?? '整理失败')
      }
    } catch (err) {
      if (err instanceof PollTimeoutError) {
        await Promise.all([
          fetchPreview(),
          fetchRecentRevisions(),
        ])
        onReflectionSettled?.()
        setNotice(`整理仍在后台运行：#${jobId}，可稍后刷新`)
        return
      }
      throw err
    }
  }

  const handleReflectionRunError = (err: unknown, fallbackLabel: string) => {
    if (err instanceof DOMException && err.name === 'AbortError') return
    setActiveJob(null)
    setError(err instanceof Error ? err.message : `${fallbackLabel}失败`)
  }

  const retryReflectionJob = async (job: Job) => {
    setJobActionBusy(true)
    setError(null)
    pollAbortRef.current?.abort()
    const controller = new AbortController()
    pollAbortRef.current = controller
    try {
      const result = await retryJob(job.id)
      setNotice(`整理已重新加入队列：#${result.job_id}`)
      setLastFailedJob(null)
      await waitForReflectionJob(result.job_id, controller.signal)
    } catch (err) {
      handleReflectionRunError(err, '重试整理')
    } finally {
      if (pollAbortRef.current === controller) pollAbortRef.current = null
      setJobActionBusy(false)
    }
  }

  const cancelReflectionJob = async (job: Job) => {
    setJobActionBusy(true)
    setError(null)
    try {
      await cancelJob(job.id)
      pollAbortRef.current?.abort()
      setActiveJob(null)
      setNotice(`整理已取消：#${job.id}`)
      await Promise.all([
        fetchPreview(),
        fetchRecentRevisions(),
      ])
      onReflectionSettled?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : '取消失败')
    } finally {
      setJobActionBusy(false)
    }
  }

  const postCount = globalScope?.post_ids.length ?? 0
  const soulInteractionCount = soulScopes.reduce((sum, scope) => sum + scope.interaction_count, 0)

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleGroup}>
          <h1 className={styles.title}>整理</h1>
          <p className={styles.subtitle}>整理公开记录与人格记忆</p>
        </div>
        <button className={styles.ghostButton} onClick={refreshPage} disabled={loading}>
          刷新
        </button>
      </header>

      {notice && <div className={styles.notice}>{notice}</div>}
      {error && <div className={styles.notice}>{error}</div>}
      {(activeJob || lastFailedJob) && (
        <ReflectionJobNotice
          activeJob={activeJob}
          failedJob={lastFailedJob}
          busy={jobActionBusy}
          onRetry={retryReflectionJob}
          onCancel={cancelReflectionJob}
        />
      )}

      {loading ? (
        <div className={styles.empty}>加载中...</div>
      ) : (
        <div className={styles.stack}>
          <section className={styles.card}>
            <div className={styles.cardHeader}>
              <h2 className={styles.cardTitle}>全局画像整理</h2>
              <button className={styles.button} onClick={runGlobal} disabled={running !== null || postCount === 0}>
                {running === 'global' ? '排队中...' : '开始整理'}
              </button>
            </div>
            <div className={styles.scopeGrid}>
              <ScopeStat value={String(postCount)} label="待处理帖子" />
              <ScopeStat value={formatDate(globalScope?.scope_start)} label="起点" />
              <ScopeStat value={formatDate(globalScope?.scope_end)} label="终点" />
            </div>
          </section>

          <section className={styles.card}>
            <div className={styles.cardHeader}>
              <h2 className={styles.cardTitle}>人格记忆整理</h2>
              <button className={styles.button} onClick={runSouls} disabled={running !== null || soulInteractionCount === 0}>
                {running === 'souls' ? '排队中...' : '开始整理'}
              </button>
            </div>
            {soulScopes.length === 0 ? (
              <div className={styles.empty}>暂无新增互动</div>
            ) : (
              <div className={styles.soulScopeList}>
                {soulScopes.map((scope) => (
                  <div key={scope.soul_name} className={styles.soulScopeItem}>
                    <span>{scope.soul_name}</span>
                    <span className={styles.meta}>{scope.interaction_count} 条互动</span>
                  </div>
                ))}
              </div>
            )}
          </section>

          <RecentRevisionsCard revisions={recentRevisions} error={revisionError} />
        </div>
      )}
    </div>
  )
}

function RecentRevisionsCard({
  revisions,
  error,
}: {
  revisions: MemoryRevisionSummary[]
  error: string | null
}) {
  return (
    <section className={styles.card}>
      <div className={styles.cardHeader}>
        <h2 className={styles.cardTitle}>最近整理</h2>
      </div>
      {error ? (
        <div className={styles.revisionEmpty}>{error}</div>
      ) : revisions.length === 0 ? (
        <div className={styles.revisionEmpty}>还没有整理产出</div>
      ) : (
        <div className={styles.revisionList}>
          {revisions.map((revision) => (
            <RevisionRow
              key={`${revision.target_type}-${revision.target_name ?? 'user'}-${revision.id}`}
              revision={revision}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function ReflectionJobNotice({
  activeJob,
  failedJob,
  busy,
  onRetry,
  onCancel,
}: {
  activeJob: Job | null
  failedJob: Job | null
  busy: boolean
  onRetry: (job: Job) => void
  onCancel: (job: Job) => void
}) {
  const job = failedJob ?? activeJob
  if (!job) return null

  const isFailed = job.status === 'failed'
  const isPending = job.status === 'pending'

  return (
    <div className={styles.notice}>
      <div className={styles.noticeRow}>
        <span>
          {isFailed
            ? `整理失败：#${job.id}`
            : `整理进行中：#${job.id}${job.error ? '，正在自动重试' : ''}`}
        </span>
        <div className={styles.noticeActions}>
          {isFailed && (
            <button className={styles.ghostButton} onClick={() => onRetry(job)} disabled={busy}>
              重试
            </button>
          )}
          {isPending && (
            <button className={styles.ghostButton} onClick={() => onCancel(job)} disabled={busy}>
              取消
            </button>
          )}
        </div>
      </div>
      {isFailed && job.error && <p className={styles.meta}>{job.error}</p>}
    </div>
  )
}

function RevisionRow({ revision }: { revision: MemoryRevisionSummary }) {
  return (
    <article className={styles.revisionRow}>
      <div className={styles.revisionBody}>
        <h3>{formatRevisionTarget(revision)}</h3>
        <p>
          <span>{formatRevisionSource(revision.source)}</span>
          <span>{summarizeRevisionPatch(revision.patch)}</span>
          <time
            dateTime={formatDateTimeAttribute(revision.created_at)}
            title={formatAbsoluteTime(revision.created_at)}
          >
            {formatSmartTime(revision.created_at)}
          </time>
        </p>
      </div>
    </article>
  )
}

function formatRevisionTarget(revision: MemoryRevisionSummary): string {
  if (revision.target_type === 'user') return '全局画像整理'
  return `${revision.target_name ?? '未命名人格'} 的人格记忆整理`
}

function formatRevisionSource(source: string): string {
  return source === 'user' ? '用户编辑' : 'AI反思'
}

function isRevisionOutput(revision: MemoryRevisionSummary): boolean {
  if (revision.source === 'system' || revision.source === 'init') return false
  if (isRecord(revision.patch) && revision.patch.op === 'init') return false
  return true
}

function summarizeRevisionPatch(patch: unknown): string {
  if (Array.isArray(patch)) return formatChangeCount(patch.length)
  if (!isRecord(patch)) return '1 条变更'

  if (Array.isArray(patch.patches)) return formatChangeCount(patch.patches.length)
  if (patch.op === 'overwrite_user_memory' || patch.op === 'overwrite_soul_memory') {
    return '全文更新'
  }
  return '1 条变更'
}

function formatChangeCount(count: number): string {
  return `${Math.max(count, 1)} 条变更`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function ScopeStat({ value, label }: { value: string; label: string }) {
  return (
    <div className={styles.scopeStat}>
      <span className={styles.scopeValue}>{value}</span>
      <span className={styles.scopeLabel}>{label}</span>
    </div>
  )
}

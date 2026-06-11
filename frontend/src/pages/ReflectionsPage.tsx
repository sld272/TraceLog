import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type Job,
  type MemoryRevisionDetail,
  type MemoryRevisionSummary,
  type ReflectionScope,
  type SoulReflectionScope,
  getProfileRevision,
  getSoulMemoryRevision,
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
const RECENT_PROFILE_REVISION_FETCH_LIMIT = 24
const RECENT_SOUL_REVISION_FETCH_LIMIT = 12
const REFLECTION_POLL_INTERVAL_MS = 3000
const REFLECTION_POLL_TIMEOUT_MS = 30000
const REVISION_GROUP_WINDOW_SECONDS = 60
const TERMINAL_JOB_STATUSES = new Set<Job['status']>(['succeeded', 'failed', 'cancelled'])

interface RevisionGroup {
  key: string
  revisions: MemoryRevisionSummary[]
  latest: MemoryRevisionSummary
  changeCount: number
}

interface RevisionDetailItem {
  revision: MemoryRevisionSummary
  detail: MemoryRevisionDetail
  previousDetail: MemoryRevisionDetail | null
}

interface RevisionDetailState {
  loading: boolean
  error: string | null
  items: RevisionDetailItem[]
}

interface RevisionChangeView {
  kind: string
  label: string
  section?: string
  anchor?: string
  before?: string
  after?: string
}

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
        listProfileRevisions(RECENT_PROFILE_REVISION_FETCH_LIMIT),
        listSouls(false),
      ])
      const soulRevisionGroups = await Promise.all(
        souls.map((soul) => listSoulMemoryRevisions(soul.name, RECENT_SOUL_REVISION_FETCH_LIMIT)),
      )
      const revisions = [...profileRevisions, ...soulRevisionGroups.flat()]
        .sort((a, b) => {
          const timeDelta = b.created_at - a.created_at
          return timeDelta === 0 ? b.id - a.id : timeDelta
        })
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
      setNotice('全局整理已开始，完成后会在下方显示更新内容')
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
      setNotice('人格记忆整理已开始，完成后会在下方显示更新内容')
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
        setNotice('整理已完成，更新内容显示在下方')
      } else if (job.status === 'cancelled') {
        setActiveJob(null)
        setNotice('整理已取消')
      } else {
        setActiveJob(null)
        setLastFailedJob(job)
        setError(null)
      }
    } catch (err) {
      if (err instanceof PollTimeoutError) {
        await Promise.all([
          fetchPreview(),
          fetchRecentRevisions(),
        ])
        onReflectionSettled?.()
        setNotice('整理仍在后台运行，可稍后刷新查看结果')
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
      setNotice('整理已重新开始')
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
      setNotice('整理已取消')
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
          <p className={styles.subtitle}>整理会让 AI 阅读新增记录，更新它对你的长期理解；每次更新的内容都会显示在下方</p>
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
              <ScopeStat value={String(postCount)} label="待处理记录" />
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
  const revisionGroups = groupRecentRevisions(revisions.filter(isRevisionOutput)).slice(0, RECENT_REVISION_LIMIT)
  const [expandedGroupKey, setExpandedGroupKey] = useState<string | null>(null)
  const [detailStates, setDetailStates] = useState<Record<string, RevisionDetailState>>({})
  const firstGroupKey = revisionGroups[0]?.key ?? null

  useEffect(() => {
    if (firstGroupKey) setExpandedGroupKey(firstGroupKey)
  }, [firstGroupKey])

  const loadGroupDetails = useCallback(async (group: RevisionGroup) => {
    setDetailStates((prev) => {
      const existing = prev[group.key]
      return {
        ...prev,
        [group.key]: existing?.items.length
          ? existing
          : { loading: true, error: null, items: [] },
      }
    })
    try {
      const items = await Promise.all(
        group.revisions.map(async (revision) => {
          const detail = await fetchRevisionDetail(revision)
          const previousRevision = findPreviousRevision(revisions, revision)
          const previousDetail = previousRevision
            ? await fetchRevisionDetail(previousRevision).catch(() => null)
            : null
          return { revision, detail, previousDetail }
        }),
      )
      setDetailStates((prev) => ({
        ...prev,
        [group.key]: { loading: false, error: null, items },
      }))
    } catch (err) {
      setDetailStates((prev) => ({
        ...prev,
        [group.key]: {
          loading: false,
          error: err instanceof Error ? err.message : '详情加载失败',
          items: prev[group.key]?.items ?? [],
        },
      }))
    }
  }, [revisions])

  useEffect(() => {
    if (!expandedGroupKey) return
    const group = revisionGroups.find((item) => item.key === expandedGroupKey)
    if (!group) return
    const state = detailStates[expandedGroupKey]
    if (state?.loading || state?.items.length) return
    void loadGroupDetails(group)
  }, [detailStates, expandedGroupKey, loadGroupDetails, revisionGroups])

  return (
    <section className={styles.card}>
      <div className={styles.cardHeader}>
        <h2 className={styles.cardTitle}>最近整理</h2>
      </div>
      {error ? (
        <div className={styles.revisionEmpty}>{error}</div>
      ) : revisionGroups.length === 0 ? (
        <div className={styles.revisionEmpty}>还没有整理产出</div>
      ) : (
        <div className={styles.revisionList}>
          {revisionGroups.map((group) => (
            <RevisionRow
              key={group.key}
              group={group}
              detailState={detailStates[group.key]}
              expanded={expandedGroupKey === group.key}
              onToggle={() => {
                setExpandedGroupKey((current) => current === group.key ? null : group.key)
                if (!detailStates[group.key]) void loadGroupDetails(group)
              }}
              onRetry={() => loadGroupDetails(group)}
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
        <span>{isFailed ? '部分整理失败' : `整理进行中...${job.error ? '（正在自动重试）' : ''}`}</span>
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
      {isFailed && job.error && (
        <details className={styles.noticeDetails}>
          <summary>诊断信息</summary>
          <p>{job.error}</p>
        </details>
      )}
    </div>
  )
}

function RevisionRow({
  group,
  expanded,
  detailState,
  onToggle,
  onRetry,
}: {
  group: RevisionGroup
  expanded: boolean
  detailState?: RevisionDetailState
  onToggle: () => void
  onRetry: () => void
}) {
  const { latest } = group
  const summary = group.revisions.length === 1
    ? summarizeSingleRevision(latest)
    : formatChangeCount(group.changeCount)

  return (
    <article className={styles.revisionRow}>
      <button className={styles.revisionToggle} onClick={onToggle} aria-expanded={expanded}>
        <div className={styles.revisionBody}>
          <h3>{formatRevisionTarget(latest)}</h3>
          <p>
            <span>{formatRevisionSource(latest.source)}</span>
            <span>{summary}</span>
            <time
              dateTime={formatDateTimeAttribute(latest.created_at)}
              title={formatAbsoluteTime(latest.created_at)}
            >
              {formatSmartTime(latest.created_at)}
            </time>
          </p>
        </div>
        <span className={styles.revisionChevron}>{expanded ? '收起' : '展开'}</span>
      </button>
      {expanded && (
        <RevisionDetails
          state={detailState}
          onRetry={onRetry}
        />
      )}
    </article>
  )
}

function RevisionDetails({
  state,
  onRetry,
}: {
  state?: RevisionDetailState
  onRetry: () => void
}) {
  if (!state || state.loading) {
    return <div className={styles.revisionDetails}>加载整理详情...</div>
  }
  if (state.error) {
    return (
      <div className={styles.revisionDetails}>
        <div className={styles.revisionDetailError}>
          <span>{state.error}</span>
          <button className={styles.ghostButton} onClick={onRetry}>重试</button>
        </div>
      </div>
    )
  }
  return (
    <div className={styles.revisionDetails}>
      {state.items.map((item) => (
        <RevisionDetailBlock key={`${item.revision.target_type}-${item.revision.target_name ?? 'user'}-${item.revision.id}`} item={item} />
      ))}
    </div>
  )
}

function RevisionDetailBlock({ item }: { item: RevisionDetailItem }) {
  const changes = describePatchChanges(item.detail.patch, item.previousDetail?.snapshot ?? '', item.detail.snapshot)

  return (
    <div className={styles.revisionDetailBlock}>
      <div className={styles.revisionDetailHeader}>
        <span>{formatRevisionTarget(item.revision)}</span>
        <time
          dateTime={formatDateTimeAttribute(item.revision.created_at)}
          title={formatAbsoluteTime(item.revision.created_at)}
        >
          {formatSmartTime(item.revision.created_at)}
        </time>
      </div>
      {changes.length === 0 ? (
        <div className={styles.revisionChange}>
          <span className={styles.revisionChangeLabel}>全文更新</span>
          <p>这次整理更新了完整记忆文本。</p>
        </div>
      ) : (
        <div className={styles.revisionChanges}>
          {changes.map((change, index) => (
            <div className={styles.revisionChange} key={`${change.kind}-${index}`}>
              <span className={styles.revisionChangeLabel}>{change.label}</span>
              {change.section && <p className={styles.revisionChangeSection}>{change.section}</p>}
              {change.before && (
                <p>
                  <strong>原来</strong>
                  {change.before}
                </p>
              )}
              {change.after && (
                <p>
                  <strong>{change.before ? '改为' : '内容'}</strong>
                  {change.after}
                </p>
              )}
              {!change.before && !change.after && change.anchor && (
                <p>
                  <strong>条目</strong>
                  {change.anchor}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
      <details className={styles.revisionSnapshot}>
        <summary>查看整理后的完整记忆</summary>
        <pre>{item.detail.snapshot}</pre>
      </details>
    </div>
  )
}

function groupRecentRevisions(revisions: MemoryRevisionSummary[]): RevisionGroup[] {
  const sorted = [...revisions].sort((a, b) => {
    const timeDelta = b.created_at - a.created_at
    return timeDelta === 0 ? b.id - a.id : timeDelta
  })
  const groups: RevisionGroup[] = []
  for (const revision of sorted) {
    const previous = groups[groups.length - 1]
    if (previous && shouldMergeRevision(previous.latest, revision)) {
      previous.revisions.push(revision)
      previous.changeCount += revisionChangeCount(revision)
      continue
    }
    groups.push({
      key: `${revision.target_type}-${revision.target_name ?? 'user'}-${revision.source}-${revision.id}`,
      revisions: [revision],
      latest: revision,
      changeCount: revisionChangeCount(revision),
    })
  }
  return groups
}

async function fetchRevisionDetail(revision: MemoryRevisionSummary): Promise<MemoryRevisionDetail> {
  if (revision.target_type === 'user') return getProfileRevision(revision.id)
  if (!revision.target_name) throw new Error('人格记忆名称缺失')
  return getSoulMemoryRevision(revision.target_name, revision.id)
}

function findPreviousRevision(
  revisions: MemoryRevisionSummary[],
  current: MemoryRevisionSummary,
): MemoryRevisionSummary | null {
  const candidates = revisions
    .filter((revision) =>
      revision.target_type === current.target_type
      && revision.target_name === current.target_name
      && (
        revision.created_at < current.created_at
        || (revision.created_at === current.created_at && revision.id < current.id)
      ),
    )
    .sort((a, b) => {
      const timeDelta = b.created_at - a.created_at
      return timeDelta === 0 ? b.id - a.id : timeDelta
    })
  return candidates[0] ?? null
}

function describePatchChanges(patch: unknown, previousSnapshot: string, currentSnapshot: string): RevisionChangeView[] {
  const patches = normalizePatchList(patch)
  const changes: RevisionChangeView[] = []
  for (const item of patches) {
    if (isRecord(item) && Array.isArray(item.ops)) {
      const section = stringField(item.section)
      for (const op of item.ops) {
        if (!isRecord(op)) continue
        const kind = stringField(op.op) ?? 'update'
        const anchor = stringField(op.anchor)
        const value = stringField(op.value)
        changes.push({
          kind,
          label: operationLabel(kind),
          section,
          anchor,
          before: anchor ? findAnchoredMemoryLine(previousSnapshot, anchor) : undefined,
          after: value || (anchor ? findAnchoredMemoryLine(currentSnapshot, anchor) : undefined),
        })
      }
      continue
    }
    if (isRecord(item)) {
      const kind = stringField(item.op) || stringField(item.type)
      if (kind && kind !== 'overwrite_user_memory' && kind !== 'overwrite_soul_memory') {
        changes.push({
          kind,
          label: operationLabel(kind),
          section: stringField(item.section),
          anchor: stringField(item.anchor),
          before: stringField(item.before),
          after: stringField(item.after) || stringField(item.value),
        })
      }
    }
  }
  return changes
}

function normalizePatchList(patch: unknown): unknown[] {
  if (Array.isArray(patch)) return patch
  if (isRecord(patch) && Array.isArray(patch.patches)) return patch.patches
  if (isRecord(patch) && (patch.op === 'overwrite_user_memory' || patch.op === 'overwrite_soul_memory')) return []
  return [patch]
}

function operationLabel(kind: string): string {
  if (kind === 'add') return '新增记忆'
  if (kind === 'update' || kind === 'revise') return '修正记忆'
  if (kind === 'remove' || kind === 'retract') return '移除记忆'
  if (kind === 'confirm') return '确认记忆'
  return '更新记忆'
}

function findAnchoredMemoryLine(snapshot: string, anchor: string): string | undefined {
  if (!anchor) return undefined
  const lines = snapshot.split(/\r?\n/)
  const marker = `<!-- id: ${anchor} -->`
  const line = lines.find((item) => item.includes(marker))
  if (!line) return undefined
  return line
    .replace(marker, '')
    .replace(/^\s*[-*]\s*/, '')
    .trim()
}

function stringField(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function shouldMergeRevision(latest: MemoryRevisionSummary, next: MemoryRevisionSummary): boolean {
  return latest.target_type === next.target_type
    && latest.target_name === next.target_name
    && latest.source === next.source
    && Math.abs(latest.created_at - next.created_at) <= REVISION_GROUP_WINDOW_SECONDS
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

function summarizeSingleRevision(revision: MemoryRevisionSummary): string {
  const patch = revision.patch
  if (isRecord(patch) && (patch.op === 'overwrite_user_memory' || patch.op === 'overwrite_soul_memory')) {
    return '全文更新'
  }
  return formatChangeCount(revisionChangeCount(revision))
}

function revisionChangeCount(revision: MemoryRevisionSummary): number {
  return patchChangeCount(revision.patch)
}

function patchChangeCount(patch: unknown): number {
  if (Array.isArray(patch)) return Math.max(patch.length, 1)
  if (!isRecord(patch)) return 1

  if (Array.isArray(patch.ops)) return Math.max(patch.ops.length, 1)
  if (Array.isArray(patch.patches)) return Math.max(patch.patches.length, 1)
  if (patch.op === 'overwrite_user_memory' || patch.op === 'overwrite_soul_memory') {
    return 1
  }
  return 1
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

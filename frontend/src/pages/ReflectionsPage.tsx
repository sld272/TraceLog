import { useCallback, useEffect, useState } from 'react'
import {
  type MemoryRevisionSummary,
  type ReflectionScope,
  type SoulReflectionScope,
  listProfileRevisions,
  listSoulMemoryRevisions,
  listSouls,
  previewGlobalReflection,
  previewSoulReflections,
  triggerGlobalReflection,
  triggerSoulReflections,
} from '@/api/client'
import {
  formatAbsoluteTime,
  formatDate,
  formatDateTimeAttribute,
  formatSmartTime,
} from '@/utils/date'
import styles from './WorkspacePages.module.css'

const RECENT_REVISION_LIMIT = 8

export function ReflectionsPage() {
  const [globalScope, setGlobalScope] = useState<ReflectionScope | null>(null)
  const [soulScopes, setSoulScopes] = useState<SoulReflectionScope[]>([])
  const [recentRevisions, setRecentRevisions] = useState<MemoryRevisionSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState<'global' | 'souls' | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [revisionError, setRevisionError] = useState<string | null>(null)

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

  const runGlobal = async () => {
    setRunning('global')
    try {
      const result = await triggerGlobalReflection()
      setNotice(`全局整理已加入队列：#${result.job_id}`)
      await refreshPage()
    } catch (err) {
      setError(err instanceof Error ? err.message : '整理失败')
    } finally {
      setRunning(null)
    }
  }

  const runSouls = async () => {
    setRunning('souls')
    try {
      const result = await triggerSoulReflections()
      setNotice(`人格记忆整理已加入队列：#${result.job_id}`)
      await refreshPage()
    } catch (err) {
      setError(err instanceof Error ? err.message : '整理失败')
    } finally {
      setRunning(null)
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

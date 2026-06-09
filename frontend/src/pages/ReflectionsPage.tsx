import { useCallback, useEffect, useState } from 'react'
import {
  type ReflectionScope,
  type SoulReflectionScope,
  previewGlobalReflection,
  previewSoulReflections,
  triggerGlobalReflection,
  triggerSoulReflections,
} from '@/api/client'
import { formatDate } from '@/utils/date'
import styles from './WorkspacePages.module.css'

export function ReflectionsPage() {
  const [globalScope, setGlobalScope] = useState<ReflectionScope | null>(null)
  const [soulScopes, setSoulScopes] = useState<SoulReflectionScope[]>([])
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState<'global' | 'souls' | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const fetchPreview = useCallback(async () => {
    try {
      setLoading(true)
      const [globalData, soulData] = await Promise.all([
        previewGlobalReflection(),
        previewSoulReflections(),
      ])
      setGlobalScope(globalData)
      setSoulScopes(soulData)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchPreview()
  }, [fetchPreview])

  const runGlobal = async () => {
    setRunning('global')
    try {
      const result = await triggerGlobalReflection()
      setNotice(`全局整理已加入队列：#${result.job_id}`)
      await fetchPreview()
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
      await fetchPreview()
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
        <button className={styles.ghostButton} onClick={fetchPreview} disabled={loading}>
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
        </div>
      )}
    </div>
  )
}

function ScopeStat({ value, label }: { value: string; label: string }) {
  return (
    <div className={styles.scopeStat}>
      <span className={styles.scopeValue}>{value}</span>
      <span className={styles.scopeLabel}>{label}</span>
    </div>
  )
}

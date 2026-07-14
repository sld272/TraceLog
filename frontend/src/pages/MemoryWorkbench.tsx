import { useCallback, useEffect, useId, useMemo, useState, type ReactNode } from 'react'
import {
  type MemoryEvidenceRef,
  type MemoryPortraitPolicy,
  type MemoryRetractReason,
  type MemoryUnit,
  type MemoryUnitDetail,
  type MemoryView,
  type MemoryViewType,
  type MemoryStatus,
  type Soul,
  createMemoryUnit,
  getJob,
  getMemoryStatus,
  getMemoryUnit,
  listMemoryUnits,
  listMemoryViews,
  listSouls,
  restoreMemoryUnit,
  resynthesizeMemoryView,
  retractMemoryUnit,
  setMemoryPromptPolicy,
  setMemoryPortraitPolicy,
  triggerMemoryReconcile,
  updateMemoryUnit,
} from '@/api/client'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Notice } from '@/components/Notice'
import { SoulAvatar } from '@/components/SoulAvatar'
import { CheckIcon, PencilIcon, TrashIcon } from '@/components/icons'
import { formatSmartTime } from '@/utils/date'
import styles from './MemoryWorkbench.module.css'

type UnitFilter = 'active' | 'pending' | 'forgotten' | 'all'

const UNIT_FILTERS: { value: UnitFilter; label: string }[] = [
  { value: 'active', label: '已记住' },
  { value: 'pending', label: '整理中' },
  { value: 'forgotten', label: '已忘记' },
  { value: 'all', label: '全部' },
]

/* 用户可见状态收敛为 5 个：已记住 / 整理中 / 收起的（不要提起）/ 已忘记·可找回 /
 * 淡出的。superseded 和模型自己的撤回是内部机制态，永不出卡片。 */
const HIDDEN_STATUSES = new Set(['retracted_by_model', 'superseded'])
const PENDING_STATUSES = new Set(['challenged', 'pending'])

function isHiddenStatus(status: string): boolean {
  return HIDDEN_STATUSES.has(status)
}

/** Challenged/pending units still await a confirm-or-drop decision. */
function isPendingStatus(status: string): boolean {
  return PENDING_STATUSES.has(status)
}

function isForgottenStatus(status: string): boolean {
  return status === 'retracted_by_user'
}

/** Wrap 「…」 spans in an accent highlight, matching the prototype portrait prose. */
function highlightQuotes(text: string): ReactNode[] {
  const parts = text.split(/(「[^」]*」)/g)
  return parts.map((part, index) =>
    part.startsWith('「') && part.endsWith('」')
      ? <strong key={index} className={styles.portraitHighlight}>{part}</strong>
      : part,
  )
}

const TYPE_LABELS: Record<string, string> = {
  preference: '偏好',
  state: '近况',
  insight: '感悟',
  relationship: '关系',
  identity: '身份',
  freeform: '其他',
}

const PORTRAIT_OPTIONS: { value: MemoryPortraitPolicy; label: string }[] = [
  { value: 'auto', label: '自动判断' },
  { value: 'force_include', label: '强制进入核心画像' },
  { value: 'force_exclude', label: '强制不进入核心画像' },
]

const SOUL_PREFIX = 'soul:'

function typeLabel(type: string): string {
  return TYPE_LABELS[type] ?? type
}

function soulNameFromScope(ownerScope: string): string {
  return ownerScope.startsWith(SOUL_PREFIX) ? ownerScope.slice(SOUL_PREFIX.length) : ownerScope
}

/** Portrait markdown carries a leading <!-- generated_by ... --> metadata block
 *  (and inline unit anchors); strip those so only the prose shows. */
function portraitProse(contentMd: string): string {
  return contentMd
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/^#+\s.*$/gm, '')
    .trim()
}

interface PortraitEntry {
  key: string
  kind: 'user' | 'soul'
  label: string
  soulName?: string
  view: MemoryView | null
  ownerScope: string
  visibilityScope: string
  viewType: MemoryViewType
  /** 尚未整理的新互动数 */
  pending: number
  /** 视图存在但已 stale（条目变化后需重综合） */
  stale: boolean
}

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms))

export function MemoryWorkbench() {
  const [views, setViews] = useState<MemoryView[]>([])
  const [souls, setSouls] = useState<Soul[]>([])
  const [memoryStatus, setMemoryStatus] = useState<MemoryStatus | null>(null)
  const [selectedKey, setSelectedKey] = useState<string>('user')
  const [units, setUnits] = useState<MemoryUnit[]>([])
  const [loadingViews, setLoadingViews] = useState(true)
  const [loadingUnits, setLoadingUnits] = useState(false)
  const [integratingKey, setIntegratingKey] = useState<string | null>(null)
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null)
  const [filter, setFilter] = useState<UnitFilter>('active')
  const [creating, setCreating] = useState(false)
  const [newContent, setNewContent] = useState('')
  const [newType, setNewType] = useState('insight')
  const [error, setError] = useState<string | null>(null)

  const entries = useMemo<PortraitEntry[]>(() => {
    const userView = views.find((view) => view.view_type === 'user_portrait') ?? null
    const pendingBuckets = memoryStatus?.pending_buckets ?? []
    const userEntry: PortraitEntry = {
      key: 'user',
      kind: 'user',
      label: '用户核心画像',
      view: userView,
      ownerScope: userView?.owner_scope ?? 'global',
      visibilityScope: userView?.visibility_scope ?? 'public',
      viewType: 'user_portrait',
      pending: pendingBuckets.filter(
        (bucket) => bucket.owner_scope === 'global' && bucket.visibility_scope === 'public',
      ).length,
      stale: userView?.status === 'stale',
    }
    const soulEntries: PortraitEntry[] = souls.map((soul) => {
      const view = views.find(
        (v) => v.view_type === 'soul_relationship_memory' && soulNameFromScope(v.owner_scope) === soul.name,
      ) ?? null
      return {
        key: `soul:${soul.name}`,
        kind: 'soul',
        label: `与 ${soul.name} 的相处记忆`,
        soulName: soul.name,
        view,
        ownerScope: view?.owner_scope ?? `soul:${soul.name}`,
        visibilityScope: view?.visibility_scope ?? 'relationship',
        viewType: 'soul_relationship_memory',
        pending: pendingBuckets.filter(
          (bucket) => bucket.owner_scope === `soul:${soul.name}`,
        ).length,
        stale: view?.status === 'stale',
      }
    })
    return [userEntry, ...soulEntries]
  }, [views, souls, memoryStatus])

  const selectedEntry = useMemo(
    () => entries.find((entry) => entry.key === selectedKey) ?? entries[0] ?? null,
    [entries, selectedKey],
  )

  const filteredUnits = useMemo(() => {
    // superseded / model-retracted units are internal mechanism states
    // (tombstones, merge losers) and never surface in the workbench
    const visible = units.filter((unit) => !isHiddenStatus(unit.status))
    if (filter === 'active') return visible.filter((unit) => unit.status === 'active')
    if (filter === 'pending') return visible.filter((unit) => isPendingStatus(unit.status))
    if (filter === 'forgotten') return visible.filter((unit) => isForgottenStatus(unit.status))
    return visible
  }, [units, filter])

  const fetchOverview = useCallback(async () => {
    const [viewData, soulData, statusData] = await Promise.all([
      listMemoryViews(),
      listSouls(true),
      getMemoryStatus(),
    ])
    return { viewData, soulData, statusData }
  }, [])

  const applyOverview = useCallback((overview: Awaited<ReturnType<typeof fetchOverview>>) => {
    setViews(overview.viewData)
    setSouls(overview.soulData)
    setMemoryStatus(overview.statusData)
  }, [])

  const loadOverview = useCallback(async () => {
    setLoadingViews(true)
    try {
      applyOverview(await fetchOverview())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载画像视图失败')
    } finally {
      setLoadingViews(false)
    }
  }, [applyOverview, fetchOverview])

  const loadUnits = useCallback(async (entry: PortraitEntry | null) => {
    if (!entry) {
      setUnits([])
      return
    }
    setLoadingUnits(true)
    try {
      const data = await listMemoryUnits({
        owner_scope: entry.ownerScope,
        visibility_scope: entry.kind === 'user' ? entry.visibilityScope : undefined,
        status: 'all',
      })
      setUnits(data)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载记忆条目失败')
    } finally {
      setLoadingUnits(false)
    }
  }, [])

  useEffect(() => {
    void loadOverview()
  }, [loadOverview])

  useEffect(() => {
    setSelectedUnitId(null)
    void loadUnits(selectedEntry)
  }, [loadUnits, selectedEntry])

  const refreshAfterMutation = useCallback(async () => {
    await loadUnits(selectedEntry)
    try {
      applyOverview(await fetchOverview())
    } catch {
      /* keep current views if the refresh fails */
    }
  }, [applyOverview, fetchOverview, loadUnits, selectedEntry])

  const handleIntegrate = async (entry: PortraitEntry) => {
    setIntegratingKey(entry.key)
    setError(null)
    try {
      if ((memoryStatus?.pending_event_count ?? 0) > 0
        || (memoryStatus?.pending_review_count ?? 0) > 0
        || (memoryStatus?.pending_relink_count ?? 0) > 0) {
        const queued = await triggerMemoryReconcile()
        const deadline = Date.now() + 90_000
        while (Date.now() < deadline) {
          await sleep(3_000)
          if (queued.job_id !== null) {
            const job = await getJob(queued.job_id)
            if (job.status === 'failed') throw new Error(job.error || '记忆对账失败')
          }
          const overview = await fetchOverview()
          applyOverview(overview)
          const stillPending = overview.statusData.pending_event_count > 0
            || overview.statusData.pending_review_count > 0
            || overview.statusData.pending_relink_count > 0
            || overview.statusData.active_jobs.length > 0
          if (!stillPending) break
        }
      } else if (entry.stale) {
        await resynthesizeMemoryView({
          owner_scope: entry.ownerScope,
          visibility_scope: entry.visibilityScope,
          view_type: entry.viewType,
        })
        applyOverview(await fetchOverview())
      }
      await loadUnits(entry)
    } catch (err) {
      setError(err instanceof Error ? err.message : '整理失败')
    } finally {
      setIntegratingKey(null)
    }
  }

  const handleCreate = async () => {
    if (!selectedEntry || !newContent.trim()) return
    setError(null)
    try {
      await createMemoryUnit({
        owner_scope: selectedEntry.ownerScope,
        visibility_scope: selectedEntry.kind === 'user'
          ? selectedEntry.visibilityScope
          : `private:soul:${selectedEntry.soulName}`,
        type: newType,
        content: newContent.trim(),
      })
      setNewContent('')
      setCreating(false)
      await refreshAfterMutation()
    } catch (err) {
      setError(err instanceof Error ? err.message : '新增记忆失败')
    }
  }

  return (
    <div className={styles.workbench}>
      <header className={styles.pageHeader}>
        <h1>记忆</h1>
        <p>拾迹记住的关于你的一切都在这里。每条记忆都能看到它从哪来，也可以随时修改，或者让 TA 忘记。</p>
      </header>

      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}

      <div className={styles.layout}>
        <aside className={styles.viewList} aria-label="画像视图">
          <h3 className={styles.viewListTitle}>画像视图</h3>
          {loadingViews ? (
            <p className={styles.muted}>加载中...</p>
          ) : (
            entries.map((entry) => {
              const active = entry.key === selectedKey
              const needsIntegration = entry.pending > 0 || entry.stale
              return (
                <button
                  key={entry.key}
                  type="button"
                  className={`${styles.viewCard} ${active ? styles.viewCardActive : ''}`}
                  onClick={() => setSelectedKey(entry.key)}
                >
                  <div className={styles.viewCardTop}>
                    {entry.kind === 'user' ? (
                      <span className={styles.meAvatar}>我</span>
                    ) : (
                      <SoulAvatar name={entry.soulName ?? ''} className={styles.soulAvatar} />
                    )}
                    <span className={styles.viewCardName}>{entry.kind === 'user' ? '我' : `与 ${entry.soulName}`}</span>
                  </div>
                  <div className={styles.viewCardMeta}>
                    <span className={`${styles.statusDot} ${needsIntegration ? styles.statusStale : styles.statusFresh}`} />
                    {needsIntegration
                      ? `有新记忆 · 待整理${entry.pending > 0 ? ` (${entry.pending})` : ''}`
                      : entry.view
                        ? '最新'
                        : '暂无相处记忆'}
                  </div>
                </button>
              )
            })
          )}
        </aside>

        <div className={styles.main}>
          {selectedEntry && (
            <section className={styles.portrait}>
              {(() => {
                const needsIntegration = selectedEntry.pending > 0 || selectedEntry.stale
                const integrating = integratingKey === selectedEntry.key
                return (
                  <>
                    <div className={`${styles.portraitStatus} ${needsIntegration ? styles.portraitStatusStale : ''}`}>
                      <span className={`${styles.statusDot} ${needsIntegration ? styles.statusStale : styles.statusFresh}`} />
                      {needsIntegration
                        ? '有新记忆 · 待整理'
                        : selectedEntry.view
                          ? `最新 · 整理于 ${formatSmartTime(selectedEntry.view.generated_at ?? selectedEntry.view.updated_at)}`
                          : '暂无相处记忆'}
                    </div>
                    <h2 className={styles.portraitTitle}>
                      {selectedEntry.label}
                      {selectedEntry.stale && (
                        <span className={styles.portraitChip}>画像更新中</span>
                      )}
                    </h2>
                    {selectedEntry.view && portraitProse(selectedEntry.view.content_md) ? (
                      <div className={styles.portraitProse}>
                        {portraitProse(selectedEntry.view.content_md).split(/\n{2,}/).map((para, index) => (
                          <p key={index}>{highlightQuotes(para)}</p>
                        ))}
                      </div>
                    ) : (
                      <p className={styles.muted}>
                        {selectedEntry.kind === 'user'
                          ? '这份画像还没有内容。条目积累到一定程度后会自动整理生成。'
                          : '还没有和 TA 的相处记忆。多聊聊、多互动，拾迹会自动整理出这段关系的记忆。'}
                      </p>
                    )}
                    {needsIntegration && (
                      <div className={styles.portraitActions}>
                        <button
                          className={styles.resynthButton}
                          onClick={() => void handleIntegrate(selectedEntry)}
                          disabled={integrating}
                        >
                          {integrating ? '整理中...' : '整理'}
                        </button>
                      </div>
                    )}
                  </>
                )
              })()}
            </section>
          )}

          <div className={styles.unitsBar}>
            <div className={styles.filterTabs} role="tablist" aria-label="记忆条目筛选">
              {UNIT_FILTERS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="tab"
                  aria-selected={filter === option.value}
                  className={`${styles.filterTab} ${filter === option.value ? styles.filterTabActive : ''}`}
                  onClick={() => setFilter(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <div>
              <span className={styles.muted}>{filteredUnits.length} 个记忆条目</span>
              <button className={styles.ghostButton} onClick={() => setCreating((value) => !value)}>
                {creating ? '取消新增' : '新增记忆'}
              </button>
            </div>
          </div>

          {creating && (
            <div className={`${styles.unit} ${styles.unitEditing}`}>
              <label className={styles.field}>
                <span>记忆内容</span>
                <textarea value={newContent} onChange={(event) => setNewContent(event.target.value)} rows={3} />
              </label>
              <label className={styles.field}>
                <span>类型</span>
                <select value={newType} onChange={(event) => setNewType(event.target.value)}>
                  {Object.entries(TYPE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </label>
              <div className={styles.editActionsRight}>
                <button className={styles.primaryButton} onClick={() => void handleCreate()}>
                  保存
                </button>
              </div>
            </div>
          )}

          {loadingUnits ? (
            <p className={styles.muted}>加载记忆条目...</p>
          ) : filteredUnits.length === 0 ? (
            <p className={styles.muted}>
              {filter === 'pending'
                ? '没有正在整理的记忆条目。'
                : filter === 'forgotten'
                  ? '没有被忘记的记忆。让 TA 忘记的内容会保留在这里，随时可以找回。'
                  : '这里还没有记忆条目。'}
            </p>
          ) : (
            filteredUnits.map((unit) => (
              <UnitCard
                key={unit.id}
                unit={unit}
                selected={selectedUnitId === unit.id}
                onSelect={() => setSelectedUnitId((prev) => (prev === unit.id ? null : unit.id))}
                onChanged={refreshAfterMutation}
                onError={setError}
              />
            ))
          )}
        </div>

        {selectedUnitId && (
          <EvidenceDrawer
            unitId={selectedUnitId}
            onClose={() => setSelectedUnitId(null)}
            onError={setError}
          />
        )}
      </div>
    </div>
  )
}

function UnitCard({
  unit,
  selected,
  onSelect,
  onChanged,
  onError,
}: {
  unit: MemoryUnit
  selected: boolean
  onSelect: () => void
  onChanged: () => Promise<void> | void
  onError: (message: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draftContent, setDraftContent] = useState(unit.content)
  const [draftType, setDraftType] = useState(unit.type)
  const [draftPortrait, setDraftPortrait] = useState<MemoryPortraitPolicy>(unit.portrait_policy)
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const muted = unit.prompt_policy === 'no_prompt'
  const inPortrait = unit.in_portrait === 1
  const forgotten = isForgottenStatus(unit.status)
  const dormant = unit.status === 'dormant'
  const statusText = inPortrait
    ? unit.portrait_policy === 'force_include'
      ? '正在塑造你的核心画像 · 已强制纳入'
      : '正在塑造你的核心画像'
    : muted
      ? '未纳入核心画像 · 已设为不要提起'
      : unit.portrait_policy === 'force_exclude'
        ? '未纳入核心画像 · 已强制排除'
        : '未纳入核心画像 · 未达到自动纳入标准'

  const openEdit = () => {
    setDraftContent(unit.content)
    setDraftType(unit.type)
    setDraftPortrait(unit.portrait_policy)
    setEditing(true)
  }

  const saveEdit = async () => {
    const content = draftContent.trim()
    if (!content) return
    setBusy(true)
    try {
      if (content !== unit.content || draftType !== unit.type) {
        await updateMemoryUnit(unit.id, { content, type: draftType })
      }
      if (draftPortrait !== unit.portrait_policy) {
        await setMemoryPortraitPolicy(unit.id, draftPortrait)
      }
      setEditing(false)
      await onChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  const toggleMute = async () => {
    setBusy(true)
    try {
      await setMemoryPromptPolicy(unit.id, muted ? 'allow' : 'no_prompt')
      await onChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '更新失败')
    } finally {
      setBusy(false)
    }
  }

  const confirmForget = async (reason: MemoryRetractReason) => {
    setConfirmDelete(false)
    setBusy(true)
    try {
      await retractMemoryUnit(unit.id, reason)
      await onChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }

  const restore = async () => {
    setBusy(true)
    try {
      await restoreMemoryUnit(unit.id)
      await onChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '找回失败')
    } finally {
      setBusy(false)
    }
  }

  if (forgotten) {
    return (
      <div className={`${styles.unit} ${styles.unitForgotten}`}>
        <p className={styles.unitContent}>{unit.content}</p>
        <div className={styles.unitMeta}>
          <span className={styles.chip}>{typeLabel(unit.type)}</span>
          <span className={styles.chipGhost}>已忘记 · 可找回</span>
        </div>
        <div className={`${styles.statusLine} ${styles.statusOut}`}>
          <span className={styles.statusOutDot} />
          <span>TA 已不再相信、不再使用这条。找回后会重新参与画像和回复。</span>
        </div>
        <div className={styles.unitActions} onClick={(e) => e.stopPropagation()}>
          <button className={styles.unitAction} onClick={restore} disabled={busy}>
            {busy ? '找回中...' : '找回'}
          </button>
        </div>
      </div>
    )
  }

  if (editing) {
    return (
      <div className={`${styles.unit} ${styles.unitEditing}`}>
        <label className={styles.field}>
          <span>记忆内容</span>
          <textarea value={draftContent} onChange={(e) => setDraftContent(e.target.value)} rows={3} />
        </label>
        <div className={styles.fieldGrid}>
          <label className={styles.field}>
            <span>类型</span>
            <select value={draftType} onChange={(e) => setDraftType(e.target.value)}>
              {Object.entries(TYPE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            <span>是否进入核心画像</span>
            <select value={draftPortrait} onChange={(e) => setDraftPortrait(e.target.value as MemoryPortraitPolicy)}>
              {PORTRAIT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
        </div>
        <p className={styles.editNote}>内容与类型可手动订正；置信度、重要度由系统综合得出，不在此处修改。</p>
        <div className={styles.editActions}>
          <button className={styles.dangerText} onClick={() => setConfirmDelete(true)} disabled={busy}>忘记</button>
          <div className={styles.editActionsRight}>
            <button className={styles.ghostButton} onClick={() => setEditing(false)} disabled={busy}>取消</button>
            <button className={styles.primaryButton} onClick={saveEdit} disabled={busy}>{busy ? '保存中...' : '保存'}</button>
          </div>
        </div>
        {confirmDelete && (
          <ForgetDialog onConfirm={confirmForget} onCancel={() => setConfirmDelete(false)} />
        )}
      </div>
    )
  }

  return (
    <div className={`${styles.unit} ${selected ? styles.unitSelected : ''}`} onClick={onSelect} role="button" tabIndex={0}>
      <p className={styles.unitContent}>{unit.content}</p>
      <div className={styles.unitMeta}>
        <span className={styles.chip}>{typeLabel(unit.type)}</span>
        <span className={styles.chipGhost}>{unit.source === 'user_authored' ? '用户编辑' : 'AI 整理'}</span>
        {isPendingStatus(unit.status) && (
          <span className={styles.chipPending} title="你最近改了相关内容，TA 正在重新核对这条">整理中</span>
        )}
        {dormant && <span className={styles.chipGhost} title="很久没提，TA 慢慢淡忘了；再提到会自然想起">淡出的</span>}
      </div>
      <div className={styles.bars}>
        <Bar label="置信度" value={unit.confidence} />
        <Bar label="重要度" value={unit.importance} warm />
      </div>
      <div className={`${styles.statusLine} ${inPortrait ? styles.statusIn : styles.statusOut}`}>
        {inPortrait ? <CheckIcon width={14} height={14} /> : <span className={styles.statusOutDot} />}
        <span>
          {isPendingStatus(unit.status)
            ? '你最近改了相关内容，TA 正在重新核对这条，核对期间暂不使用'
            : statusText}
        </span>
      </div>
      <div className={styles.unitActions} onClick={(e) => e.stopPropagation()}>
        <button className={styles.unitAction} onClick={openEdit} disabled={busy}>
          <PencilIcon /> 编辑
        </button>
        <button className={`${styles.unitAction} ${muted ? styles.unitActionOn : ''}`} onClick={toggleMute} disabled={busy}>
          {muted ? '恢复提及' : '不要提起'}
        </button>
        <button className={`${styles.unitAction} ${styles.unitActionDanger}`} onClick={() => setConfirmDelete(true)} disabled={busy}>
          <TrashIcon /> 忘记
        </button>
      </div>
      {confirmDelete && (
        <ForgetDialog onConfirm={confirmForget} onCancel={() => setConfirmDelete(false)} />
      )}
    </div>
  )
}

/* 忘记 = 后端的用户撤回：只翻状态、更新画像，原始帖子/聊天记录都还在。
 * 默认选"过时"：误标过时可逆（记忆可凭新证据重新长出），误标"没这回事"
 * 会永久阻止这条信念再生，代价不对称。 */
function ForgetDialog({
  onConfirm,
  onCancel,
}: {
  onConfirm: (reason: MemoryRetractReason) => void
  onCancel: () => void
}) {
  const [reason, setReason] = useState<MemoryRetractReason>('outdated')
  const radioGroup = useId()
  return (
    <ConfirmDialog
      isOpen
      title="忘记这条记忆？"
      message="TA 会立刻不再相信、不再使用这条，画像随之更新。你的原始帖子和聊天记录不受影响，这条之后也随时可以在「已忘记」里找回。"
      confirmText="忘记"
      cancelText="取消"
      danger
      onConfirm={() => onConfirm(reason)}
      onCancel={onCancel}
    >
      <div className={styles.forgetOptions}>
        <label className={styles.forgetOption}>
          <input
            type="radio"
            name={radioGroup}
            checked={reason === 'outdated'}
            onChange={() => setReason('outdated')}
          />
          <span>以前是这样，但已经过时了</span>
        </label>
        <label className={styles.forgetOption}>
          <input
            type="radio"
            name={radioGroup}
            checked={reason === 'false'}
            onChange={() => setReason('false')}
          />
          <span>TA 理解错了，根本没这回事</span>
        </label>
      </div>
      <p className={styles.forgetHint}>如果只是不想让 TA 主动提起，用「不要提起」就够了。</p>
    </ConfirmDialog>
  )
}

function Bar({ label, value, warm = false }: { label: string; value: number; warm?: boolean }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100)
  return (
    <div className={styles.bar}>
      <div className={styles.barLabel}>
        <span>{label}</span>
        <span>{pct}%</span>
      </div>
      <div className={styles.barTrack}>
        <div className={`${styles.barFill} ${warm ? styles.barFillWarm : ''}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function EvidenceDrawer({
  unitId,
  onClose,
  onError,
}: {
  unitId: string
  onClose: () => void
  onError: (message: string) => void
}) {
  const [detail, setDetail] = useState<MemoryUnitDetail | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setDetail(null)
    getMemoryUnit(unitId)
      .then((data) => {
        if (!cancelled) setDetail(data)
      })
      .catch((err) => {
        if (!cancelled) onError(err instanceof Error ? err.message : '加载证据失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [unitId, onError])

  return (
    <aside className={styles.drawer} aria-label="证据追溯">
      <div className={styles.drawerHeader}>
        <h3>证据追溯</h3>
        <button className={styles.drawerClose} onClick={onClose} title="关闭">×</button>
      </div>
      <p className={styles.drawerSub}>这条记忆背后的原始证据</p>
      {loading ? (
        <p className={styles.muted}>加载中...</p>
      ) : !detail ? (
        <p className={styles.muted}>没有可显示的证据。</p>
      ) : (
        <>
          <div className={styles.drawerUnit}>{detail.content}</div>
          <div className={styles.evHead}>证据 · {detail.evidence.length} 条</div>
          {detail.evidence.length === 0 ? (
            <p className={styles.muted}>这条记忆暂时没有关联到原始证据。</p>
          ) : (
            detail.evidence.map((ev) => <EvidenceRow key={ev.event_id} ev={ev} />)
          )}
        </>
      )}
    </aside>
  )
}

const SOURCE_TYPE_LABELS: Record<string, string> = {
  post: '公开帖子',
  post_vision: '帖子里的图片',
  comment_message: '评论区',
  comment_relationship: '评论区',
  chat_message: '私聊',
}

function EvidenceRow({ ev }: { ev: MemoryEvidenceRef }) {
  const stateLabel = ev.review_pending
    ? '核对中'
    : ev.state === 'superseded'
      ? '已有新版本'
      : ev.state === 'deleted'
        ? '原文已删除'
        : '当前'
  const dim = ev.state === 'superseded' || ev.state === 'deleted'
  return (
    <div className={`${styles.ev} ${dim ? styles.evDim : ''}`}>
      <div className={styles.evSrc}>
        <span>{SOURCE_TYPE_LABELS[ev.source_type] ?? '记录'}</span>
        <span className={styles.evTag}>{stateLabel}</span>
      </div>
      <div className={styles.evText}>{ev.content}</div>
    </div>
  )
}

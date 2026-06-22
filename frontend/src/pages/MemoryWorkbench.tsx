import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  type MemoryEvidenceRef,
  type MemoryProfilePolicy,
  type MemoryUnit,
  type MemoryUnitDetail,
  type MemoryView,
  type MemoryViewType,
  type ReflectionScope,
  type Soul,
  type SoulReflectionScope,
  getMemoryUnit,
  listMemoryUnits,
  listMemoryViews,
  listSouls,
  previewGlobalReflection,
  previewSoulReflections,
  resynthesizeMemoryView,
  retractMemoryUnit,
  setMemoryPromptPolicy,
  setMemoryProfilePolicy,
  triggerGlobalReflection,
  triggerSoulReflections,
  updateMemoryUnit,
} from '@/api/client'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Notice } from '@/components/Notice'
import { SoulAvatar } from '@/components/SoulAvatar'
import { CheckIcon, PencilIcon, TrashIcon } from '@/components/icons'
import { formatSmartTime } from '@/utils/date'
import styles from './MemoryWorkbench.module.css'

type UnitFilter = 'active' | 'pending' | 'all'

const UNIT_FILTERS: { value: UnitFilter; label: string }[] = [
  { value: 'active', label: '进行中' },
  { value: 'pending', label: '待确认 · 迁移' },
  { value: 'all', label: '全部' },
]

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

const PROFILE_OPTIONS: { value: MemoryProfilePolicy; label: string }[] = [
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
  const [globalPreview, setGlobalPreview] = useState<ReflectionScope | null>(null)
  const [soulPreviews, setSoulPreviews] = useState<SoulReflectionScope[]>([])
  const [selectedKey, setSelectedKey] = useState<string>('user')
  const [units, setUnits] = useState<MemoryUnit[]>([])
  const [loadingViews, setLoadingViews] = useState(true)
  const [loadingUnits, setLoadingUnits] = useState(false)
  const [integratingKey, setIntegratingKey] = useState<string | null>(null)
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null)
  const [filter, setFilter] = useState<UnitFilter>('active')
  const [error, setError] = useState<string | null>(null)

  const entries = useMemo<PortraitEntry[]>(() => {
    const userView = views.find((view) => view.view_type === 'user_md') ?? null
    const userEntry: PortraitEntry = {
      key: 'user',
      kind: 'user',
      label: '用户核心画像',
      view: userView,
      ownerScope: userView?.owner_scope ?? 'global',
      visibilityScope: userView?.visibility_scope ?? 'public',
      viewType: 'user_md',
      pending: globalPreview?.post_ids.length ?? 0,
      stale: userView?.status === 'stale',
    }
    const soulEntries: PortraitEntry[] = souls.map((soul) => {
      const view = views.find(
        (v) => v.view_type === 'soul_relationship_memory' && soulNameFromScope(v.owner_scope) === soul.name,
      ) ?? null
      const preview = soulPreviews.find((scope) => scope.soul_name === soul.name)
      return {
        key: `soul:${soul.name}`,
        kind: 'soul',
        label: `与 ${soul.name} 的相处记忆`,
        soulName: soul.name,
        view,
        ownerScope: view?.owner_scope ?? `soul:${soul.name}`,
        visibilityScope: view?.visibility_scope ?? 'relationship',
        viewType: 'soul_relationship_memory',
        pending: Math.max(preview?.interaction_count ?? 0, 0),
        stale: view?.status === 'stale',
      }
    })
    return [userEntry, ...soulEntries]
  }, [views, souls, globalPreview, soulPreviews])

  const selectedEntry = useMemo(
    () => entries.find((entry) => entry.key === selectedKey) ?? entries[0] ?? null,
    [entries, selectedKey],
  )

  const filteredUnits = useMemo(() => {
    if (filter === 'all') return units
    if (filter === 'active') return units.filter((unit) => unit.status === 'active')
    return units.filter((unit) => unit.status !== 'active')
  }, [units, filter])

  const fetchOverview = useCallback(async () => {
    const [viewData, soulData, globalData, soulPreviewData] = await Promise.all([
      listMemoryViews(),
      listSouls(true),
      previewGlobalReflection().catch(() => null),
      previewSoulReflections().catch(() => [] as SoulReflectionScope[]),
    ])
    return { viewData, soulData, globalData, soulPreviewData }
  }, [])

  const applyOverview = useCallback((overview: Awaited<ReturnType<typeof fetchOverview>>) => {
    setViews(overview.viewData)
    setSouls(overview.soulData)
    setGlobalPreview(overview.globalData)
    setSoulPreviews(overview.soulPreviewData)
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
        visibility_scope: entry.visibilityScope,
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
      if (entry.pending > 0) {
        if (entry.kind === 'user') {
          await triggerGlobalReflection()
        } else {
          await triggerSoulReflections()
        }
        // 整理在后台异步执行，轮询直到该范围不再有待整理互动
        const deadline = Date.now() + 90_000
        while (Date.now() < deadline) {
          await sleep(3_000)
          const overview = await fetchOverview()
          applyOverview(overview)
          const stillPending = entry.kind === 'user'
            ? (overview.globalData?.post_ids.length ?? 0) > 0
            : (overview.soulPreviewData.find((s) => s.soul_name === entry.soulName)?.interaction_count ?? 0) > 0
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

  return (
    <div className={styles.workbench}>
      <header className={styles.pageHeader}>
        <h1>记忆</h1>
        <p>拾迹记住的关于你的一切都在这里。每条记忆都能看到它从哪来，也可以随时修改或删除。</p>
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
                    <h2 className={styles.portraitTitle}>{selectedEntry.label}</h2>
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
            <span className={styles.muted}>{filteredUnits.length} 个记忆条目</span>
          </div>

          {loadingUnits ? (
            <p className={styles.muted}>加载记忆条目...</p>
          ) : filteredUnits.length === 0 ? (
            <p className={styles.muted}>
              {filter === 'pending' ? '没有待确认或待迁移的记忆条目。' : '这里还没有记忆条目。'}
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
  const [draftProfile, setDraftProfile] = useState<MemoryProfilePolicy>(unit.profile_policy)
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const muted = unit.prompt_policy === 'no_prompt'
  const inPortrait = unit.in_md_slice === 1
  const statusText = inPortrait
    ? unit.profile_policy === 'force_include'
      ? '正在塑造你的核心画像 · 已强制纳入'
      : '正在塑造你的核心画像'
    : muted
      ? '未纳入核心画像 · 已设为不要提起'
      : unit.profile_policy === 'force_exclude'
        ? '未纳入核心画像 · 已强制排除'
        : '未纳入核心画像 · 未达到自动纳入标准'

  const openEdit = () => {
    setDraftContent(unit.content)
    setDraftType(unit.type)
    setDraftProfile(unit.profile_policy)
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
      if (draftProfile !== unit.profile_policy) {
        await setMemoryProfilePolicy(unit.id, draftProfile)
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

  const confirmRetract = async () => {
    setConfirmDelete(false)
    setBusy(true)
    try {
      await retractMemoryUnit(unit.id, 'false')
      await onChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setBusy(false)
    }
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
            <select value={draftProfile} onChange={(e) => setDraftProfile(e.target.value as MemoryProfilePolicy)}>
              {PROFILE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
        </div>
        <p className={styles.editNote}>内容与类型可手动订正；置信度、重要度由系统综合得出，不在此处修改。</p>
        <div className={styles.editActions}>
          <button className={styles.dangerText} onClick={() => setConfirmDelete(true)} disabled={busy}>删除</button>
          <div className={styles.editActionsRight}>
            <button className={styles.ghostButton} onClick={() => setEditing(false)} disabled={busy}>取消</button>
            <button className={styles.primaryButton} onClick={saveEdit} disabled={busy}>{busy ? '保存中...' : '保存'}</button>
          </div>
        </div>
        {confirmDelete && (
          <ConfirmDialog
            isOpen
            title="删除这条记忆？"
            message="删除后，这条记忆及其全部原始证据会被一并移除，且无法恢复。下次整理时，画像会在没有它的情况下重新生成。如果只是不希望 AI 主动提起，用「不要提起」即可。"
            confirmText="确认删除"
            cancelText="取消"
            danger
            onConfirm={confirmRetract}
            onCancel={() => setConfirmDelete(false)}
          />
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
      </div>
      <div className={styles.bars}>
        <Bar label="置信度" value={unit.confidence} />
        <Bar label="重要度" value={unit.importance} warm />
      </div>
      <div className={`${styles.statusLine} ${inPortrait ? styles.statusIn : styles.statusOut}`}>
        {inPortrait ? <CheckIcon width={14} height={14} /> : <span className={styles.statusOutDot} />}
        <span>{statusText}</span>
      </div>
      <div className={styles.unitActions} onClick={(e) => e.stopPropagation()}>
        <button className={styles.unitAction} onClick={openEdit} disabled={busy}>
          <PencilIcon /> 编辑
        </button>
        <button className={`${styles.unitAction} ${muted ? styles.unitActionOn : ''}`} onClick={toggleMute} disabled={busy}>
          {muted ? '恢复提及' : '不要提起'}
        </button>
        <button className={`${styles.unitAction} ${styles.unitActionDanger}`} onClick={() => setConfirmDelete(true)} disabled={busy}>
          <TrashIcon /> 删除
        </button>
      </div>
      {confirmDelete && (
        <ConfirmDialog
          isOpen
          title="删除这条记忆？"
          message="删除后，这条记忆及其全部原始证据会被一并移除，且无法恢复。下次整理时，画像会在没有它的情况下重新生成。如果只是不希望 AI 主动提起，用「不要提起」即可。"
          confirmText="确认删除"
          cancelText="取消"
          danger
          onConfirm={confirmRetract}
          onCancel={() => setConfirmDelete(false)}
        />
      )}
    </div>
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

function EvidenceRow({ ev }: { ev: MemoryEvidenceRef }) {
  const stateLabel = ev.review_pending
    ? '待重新关联'
    : ev.state === 'superseded'
      ? '已被取代'
      : ev.state === 'deleted'
        ? '已删除'
        : '当前'
  const dim = ev.state === 'superseded' || ev.state === 'deleted'
  return (
    <div className={`${styles.ev} ${dim ? styles.evDim : ''}`}>
      <div className={styles.evSrc}>
        <span>{ev.source_channel} · {ev.source_type}</span>
        <span className={styles.evTag}>{stateLabel}</span>
      </div>
      <div className={styles.evText}>{ev.content}</div>
    </div>
  )
}

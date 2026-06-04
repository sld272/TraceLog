import { FormEvent, useEffect, useMemo, useState } from 'react'
import {
  type MemoryRevisionDetail,
  type MemoryRevisionSummary,
  type Soul,
  type WorkspaceStatus,
  getProfile,
  getProfileRevision,
  getSoulMemory,
  getSoulMemoryRevision,
  listProfileRevisions,
  listSoulMemoryRevisions,
  updateProfile,
  updateSoulMemory,
} from '@/api/client'
import workspaceStyles from './WorkspacePages.module.css'
import styles from './SettingsPage.module.css'

type MemoryKind = 'profile' | 'soul'
type EditorMode = 'structured' | 'markdown'

interface MemoryObject {
  id: string
  kind: MemoryKind
  label: string
  detail: string
  path: string
  enabled?: boolean
}

interface SectionSpec {
  title: string
  sensitivity?: 'high' | 'normal' | 'low'
}

interface ParsedSection {
  title: string
  body: string
}

const PROFILE_SECTIONS: SectionSpec[] = [
  { title: '基本信息', sensitivity: 'high' },
  { title: '身份与角色', sensitivity: 'high' },
  { title: '性格与倾向', sensitivity: 'normal' },
  { title: '技能与专长', sensitivity: 'normal' },
  { title: '兴趣与习惯', sensitivity: 'normal' },
  { title: '核心人际关系', sensitivity: 'normal' },
  { title: '长期目标', sensitivity: 'normal' },
  { title: '当前状态与关注', sensitivity: 'low' },
]

const SOUL_MEMORY_SECTIONS: SectionSpec[] = [
  { title: '对用户的理解' },
  { title: '我们之间的互动约定' },
  { title: '私聊沉淀' },
]

export function MemorySettingsPanel({
  souls,
  workspaceStatus,
}: {
  souls: Soul[]
  workspaceStatus: WorkspaceStatus | null
}) {
  const memoryObjects = useMemo(
    () => buildMemoryObjects(souls, workspaceStatus),
    [souls, workspaceStatus],
  )
  const [selectedObjectId, setSelectedObjectId] = useState('profile')
  const [editorMode, setEditorMode] = useState<EditorMode>('structured')
  const [savedContent, setSavedContent] = useState('')
  const [draftContent, setDraftContent] = useState('')
  const [revisions, setRevisions] = useState<MemoryRevisionSummary[]>([])
  const [snapshot, setSnapshot] = useState<MemoryRevisionDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loadingSnapshotId, setLoadingSnapshotId] = useState<number | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const selectedObject = memoryObjects.find((item) => item.id === selectedObjectId)
    ?? buildProfileMemoryObject(workspaceStatus)
  const isDirty = draftContent !== savedContent
  const displaySections = useMemo(
    () => getDisplaySections(selectedObject.kind, draftContent),
    [selectedObject.kind, draftContent],
  )
  const sourceSummary = useMemo(() => summarizeSources(revisions), [revisions])

  useEffect(() => {
    if (!memoryObjects.some((item) => item.id === selectedObjectId)) {
      setSelectedObjectId('profile')
    }
  }, [memoryObjects, selectedObjectId])

  useEffect(() => {
    void loadMemory(selectedObject)
  }, [selectedObject.id])

  const handleSelectObject = (objectId: string) => {
    if (objectId === selectedObject.id) return
    if (isDirty && !window.confirm('当前记忆有未保存草稿，切换后会放弃这些修改。继续切换？')) {
      return
    }
    setSelectedObjectId(objectId)
  }

  const handleReload = () => {
    if (isDirty && !window.confirm('当前记忆有未保存草稿，刷新会放弃这些修改。继续刷新？')) {
      return
    }
    void loadMemory(selectedObject)
  }

  const handleSectionChange = (title: string, body: string) => {
    setDraftContent((content) => updateSectionBody(content, title, body))
  }

  const handleSave = async (event: FormEvent) => {
    event.preventDefault()
    setSaving(true)
    setNotice(null)
    setError(null)
    try {
      const content = draftContent.trimEnd() + '\n'
      const saved = selectedObject.kind === 'profile'
        ? (await updateProfile(content)).content
        : (await updateSoulMemory(selectedObject.label, content)).content
      setSavedContent(saved)
      setDraftContent(saved)
      setSnapshot(null)
      setNotice('记忆已保存，新的版本记录已经写入。')
      setRevisions(await fetchRevisions(selectedObject))
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存记忆失败')
    } finally {
      setSaving(false)
    }
  }

  const handleViewRevision = async (revision: MemoryRevisionSummary) => {
    setLoadingSnapshotId(revision.id)
    setError(null)
    try {
      const detail = selectedObject.kind === 'profile'
        ? await getProfileRevision(revision.id)
        : await getSoulMemoryRevision(selectedObject.label, revision.id)
      setSnapshot(detail)
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取历史版本失败')
    } finally {
      setLoadingSnapshotId(null)
    }
  }

  const handleCopySnapshot = () => {
    if (!snapshot) return
    setDraftContent(snapshot.snapshot)
    setNotice('历史快照已复制到当前草稿，保存后才会覆盖现有记忆。')
  }

  async function loadMemory(object: MemoryObject) {
    setLoading(true)
    setNotice(null)
    setError(null)
    setSnapshot(null)
    try {
      const [content, nextRevisions] = await Promise.all([
        fetchContent(object),
        fetchRevisions(object),
      ])
      setSavedContent(content)
      setDraftContent(content)
      setRevisions(nextRevisions)
      setEditorMode('structured')
    } catch (err) {
      setSavedContent('')
      setDraftContent('')
      setRevisions([])
      setError(err instanceof Error ? err.message : '读取记忆失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className={styles.settingsStack}>
      {error && <div className={workspaceStyles.notice}>{error}</div>}
      {notice && <div className={styles.successNotice}>{notice}</div>}

      <section className={styles.memoryHero}>
        <div>
          <h2 className={styles.sectionTitle}>记忆工作台</h2>
          <p className={styles.sectionMeta}>用户画像和每个 SOUL 的相处记忆分开管理；人格设定仍留在“人格”页。</p>
        </div>
        <div className={styles.memoryStats}>
          <MemoryStat label="用户画像" value="1" />
          <MemoryStat label="人格记忆" value={souls.length} />
          <MemoryStat label="当前版本" value={revisions.length} />
          <MemoryStat label="待审核" value="预留" muted />
        </div>
      </section>

      <div className={styles.memoryWorkbench}>
        <aside className={styles.memorySidebar} aria-label="记忆对象">
          <div className={styles.memorySidebarHeader}>
            <span>记忆目录</span>
            <button className={styles.iconButton} type="button" onClick={handleReload} title="刷新当前记忆">
              ↻
            </button>
          </div>
          <div className={styles.memoryObjectList}>
            {memoryObjects.map((object) => (
              <button
                key={object.id}
                className={`${styles.memoryObject} ${object.id === selectedObject.id ? styles.memoryObjectActive : ''}`}
                type="button"
                onClick={() => handleSelectObject(object.id)}
              >
                <span className={styles.memoryObjectType}>{object.kind === 'profile' ? '用户画像' : '人格记忆'}</span>
                <span className={styles.memoryObjectName}>{object.label}</span>
                <span className={styles.memoryObjectMeta}>
                  {object.kind === 'profile' ? object.detail : `${object.detail} · ${object.enabled ? '启用' : '停用'}`}
                </span>
              </button>
            ))}
          </div>
        </aside>

        <form className={styles.memoryEditor} onSubmit={handleSave}>
          <div className={styles.memoryEditorHeader}>
            <div>
              <div className={styles.memoryEyebrow}>{selectedObject.kind === 'profile' ? '用户画像' : 'SOUL 相处记忆'}</div>
              <h2 className={styles.memoryTitle}>{selectedObject.label}</h2>
              <p className={styles.memoryPath}>{selectedObject.path}</p>
            </div>
            <div className={styles.memoryHeaderControls}>
              {isDirty && <span className={styles.dirtyBadge}>未保存</span>}
              <div className={styles.modeTabs} role="tablist" aria-label="记忆编辑方式">
                <button
                  className={`${styles.modeTab} ${editorMode === 'structured' ? styles.modeTabActive : ''}`}
                  type="button"
                  role="tab"
                  aria-selected={editorMode === 'structured'}
                  onClick={() => setEditorMode('structured')}
                >
                  结构化
                </button>
                <button
                  className={`${styles.modeTab} ${editorMode === 'markdown' ? styles.modeTabActive : ''}`}
                  type="button"
                  role="tab"
                  aria-selected={editorMode === 'markdown'}
                  onClick={() => setEditorMode('markdown')}
                >
                  Markdown
                </button>
              </div>
            </div>
          </div>

          <div className={styles.memorySummaryRow}>
            <span>来源：{sourceSummary}</span>
            <span>{selectedObject.kind === 'profile' ? '保存到 user.md' : '保存到 soul_memories'}</span>
          </div>

          {loading ? (
            <div className={workspaceStyles.empty}>加载记忆中...</div>
          ) : editorMode === 'structured' ? (
            <div className={styles.memorySectionList}>
              {displaySections.map((section) => (
                <label className={styles.memorySection} key={section.title}>
                  <span className={styles.memorySectionHeader}>
                    <span>{section.title}</span>
                    {section.sensitivity && (
                      <span className={`${styles.sensitivityBadge} ${styles[`sensitivity_${section.sensitivity}`]}`}>
                        {sensitivityLabel(section.sensitivity)}
                      </span>
                    )}
                  </span>
                  <textarea
                    value={section.body}
                    onChange={(event) => handleSectionChange(section.title, event.target.value)}
                    placeholder="这里还没有沉淀内容。"
                    rows={section.body.trim() ? 4 : 3}
                  />
                </label>
              ))}
            </div>
          ) : (
            <label className={styles.textareaField}>
              <span>Markdown 全文</span>
              <textarea
                className={styles.memoryMarkdownArea}
                value={draftContent}
                onChange={(event) => setDraftContent(event.target.value)}
                rows={22}
              />
            </label>
          )}

          <div className={styles.memorySaveBar}>
            <span className={styles.saveHint}>
              {selectedObject.kind === 'profile'
                ? '高敏感画像应由用户明确确认；手动保存会直接写入 revision。'
                : '这里只编辑相处记忆，不改变 SOUL 的人格 Markdown。'}
            </span>
            <div className={styles.formActions}>
              <button
                className={workspaceStyles.ghostButton}
                type="button"
                onClick={() => setDraftContent(savedContent)}
                disabled={!isDirty || saving}
              >
                放弃草稿
              </button>
              <button
                className={workspaceStyles.button}
                type="submit"
                disabled={!isDirty || saving || !draftContent.trim()}
              >
                {saving ? '保存中...' : '保存记忆'}
              </button>
            </div>
          </div>
        </form>

        <section className={styles.memoryHistory}>
          <div className={styles.sectionHeader}>
            <div>
              <h2 className={styles.sectionTitle}>历史</h2>
              <p className={styles.sectionMeta}>最近 20 条版本记录。</p>
            </div>
          </div>
          <div className={styles.revisionList}>
            {revisions.map((revision) => (
              <button
                className={`${styles.revisionItem} ${snapshot?.id === revision.id ? styles.revisionItemActive : ''}`}
                key={revision.id}
                type="button"
                onClick={() => handleViewRevision(revision)}
              >
                <span className={styles.revisionMain}>
                  <span>{sourceLabel(revision.source)}</span>
                  <span>{patchSummary(revision.patch)}</span>
                </span>
                <span className={styles.revisionMeta}>
                  {loadingSnapshotId === revision.id ? '读取中...' : formatDateTime(revision.created_at)}
                </span>
              </button>
            ))}
            {revisions.length === 0 && <div className={workspaceStyles.empty}>暂无历史版本</div>}
          </div>
          {snapshot && (
            <div className={styles.snapshotPanel}>
              <div className={styles.snapshotHeader}>
                <span>快照 #{snapshot.id}</span>
                <button className={workspaceStyles.ghostButton} type="button" onClick={handleCopySnapshot}>
                  复制到草稿
                </button>
              </div>
              <pre>{snapshot.snapshot}</pre>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function MemoryStat({ label, value, muted = false }: { label: string; value: string | number; muted?: boolean }) {
  return (
    <div className={`${styles.memoryStat} ${muted ? styles.memoryStatMuted : ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

async function fetchContent(object: MemoryObject): Promise<string> {
  if (object.kind === 'profile') {
    return (await getProfile()).content
  }
  return (await getSoulMemory(object.label)).content
}

async function fetchRevisions(object: MemoryObject): Promise<MemoryRevisionSummary[]> {
  if (object.kind === 'profile') {
    return listProfileRevisions()
  }
  return listSoulMemoryRevisions(object.label)
}

function buildMemoryObjects(souls: Soul[], status: WorkspaceStatus | null): MemoryObject[] {
  return [
    buildProfileMemoryObject(status),
    ...souls.map((soul) => ({
      id: `soul:${soul.name}`,
      kind: 'soul' as const,
      label: soul.name,
      detail: soul.description || '暂无描述',
      path: status ? `${status.soul_memories_dir}/${soul.name}.md` : `workspace/soul_memories/${soul.name}.md`,
      enabled: soul.enabled,
    })),
  ]
}

function buildProfileMemoryObject(status: WorkspaceStatus | null): MemoryObject {
  return {
    id: 'profile',
    kind: 'profile',
    label: '用户画像',
    detail: '全局理解',
    path: status?.user_memory_path ?? 'workspace/user.md',
  }
}

function getDisplaySections(kind: MemoryKind, content: string) {
  const specs = kind === 'profile' ? PROFILE_SECTIONS : SOUL_MEMORY_SECTIONS
  const parsed = parseMarkdownSections(content)
  const sectionMap = new Map(parsed.sections.map((section) => [section.title, section.body]))
  return specs.map((spec) => ({
    ...spec,
    body: sectionMap.get(spec.title) ?? '',
  }))
}

function parseMarkdownSections(content: string): { prefix: string; sections: ParsedSection[] } {
  const lines = content.split(/\r?\n/)
  const sections: ParsedSection[] = []
  let firstSectionIndex = lines.length
  let currentTitle: string | null = null
  let currentBody: string[] = []

  lines.forEach((line, index) => {
    const match = /^##\s+(.+?)\s*$/.exec(line)
    const title = match?.[1]?.trim()
    if (!title) {
      if (currentTitle !== null) {
        currentBody.push(line)
      }
      return
    }
    if (sections.length === 0 && currentTitle === null) {
      firstSectionIndex = index
    }
    if (currentTitle !== null) {
      sections.push({ title: currentTitle, body: currentBody.join('\n').trimEnd() })
    }
    currentTitle = title
    currentBody = []
  })

  if (currentTitle !== null) {
    sections.push({ title: currentTitle, body: currentBody.join('\n').trimEnd() })
  }

  return {
    prefix: lines.slice(0, firstSectionIndex).join('\n').trimEnd(),
    sections,
  }
}

function updateSectionBody(content: string, title: string, body: string): string {
  const parsed = parseMarkdownSections(content)
  let found = false
  const sections = parsed.sections.map((section) => {
    if (section.title !== title) return section
    found = true
    return { ...section, body }
  })
  if (!found) {
    sections.push({ title, body })
  }
  return buildMarkdown(parsed.prefix, sections)
}

function buildMarkdown(prefix: string, sections: ParsedSection[]): string {
  const parts = []
  const trimmedPrefix = prefix.trimEnd()
  if (trimmedPrefix) {
    parts.push(trimmedPrefix)
  }
  sections.forEach((section) => {
    parts.push(`## ${section.title}\n${section.body.trimEnd()}`)
  })
  return `${parts.join('\n\n').trimEnd()}\n`
}

function sensitivityLabel(value: NonNullable<SectionSpec['sensitivity']>): string {
  if (value === 'high') return '高敏感'
  if (value === 'low') return '低敏感'
  return '普通'
}

function sourceLabel(source: string): string {
  if (source === 'user') return '用户手动'
  if (source === 'reflector') return '全局反思'
  if (source === 'soul_deep_reflector') return 'SOUL 深反思'
  if (source === 'system' || source === 'init') return '系统初始化'
  return source
}

function summarizeSources(revisions: MemoryRevisionSummary[]): string {
  if (revisions.length === 0) return '暂无版本记录'
  const counts = new Map<string, number>()
  revisions.forEach((revision) => {
    const label = sourceLabel(revision.source)
    counts.set(label, (counts.get(label) ?? 0) + 1)
  })
  return Array.from(counts.entries())
    .slice(0, 3)
    .map(([label, count]) => `${label} ${count}`)
    .join(' · ')
}

function patchSummary(patch: unknown): string {
  if (!patch || typeof patch !== 'object') return '记忆变更'
  const item = patch as { op?: unknown; section?: unknown; ops?: unknown }
  if (item.op === 'overwrite_user_memory') return '覆盖用户画像'
  if (item.op === 'overwrite_soul_memory') return '覆盖人格记忆'
  if (item.op === 'init') return '初始化'
  if (typeof item.section === 'string') {
    return `更新 ${item.section}`
  }
  return Array.isArray(item.ops) ? '结构化变更' : '记忆变更'
}

function formatDateTime(value: number): string {
  const date = new Date(value * 1000)
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

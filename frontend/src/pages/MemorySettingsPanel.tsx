import { FormEvent, useEffect, useMemo, useState } from 'react'
import {
  type Soul,
  type WorkspaceStatus,
  getProfile,
  getSoulMemory,
  updateProfile,
  updateSoulMemory,
} from '@/api/client'
import { Notice } from '@/components/Notice'
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
}

interface ParsedSection {
  title: string
  body: string
}

const PROFILE_SECTIONS: SectionSpec[] = [
  { title: '基本信息' },
  { title: '身份与角色' },
  { title: '性格与倾向' },
  { title: '技能与专长' },
  { title: '兴趣与习惯' },
  { title: '核心人际关系' },
  { title: '长期目标' },
  { title: '当前状态与关注' },
]

const SOUL_MEMORY_SECTIONS: SectionSpec[] = [
  { title: '对用户的理解' },
  { title: '我们之间的互动约定' },
  { title: '私聊沉淀' },
]

const MEMORY_ANCHOR_RE = /<!--\s*id:\s*[A-Za-z0-9_-]+\s*-->/
const MEMORY_ANCHOR_GLOBAL_RE = /\s*<!--\s*id:\s*[A-Za-z0-9_-]+\s*-->/g
const MEMORY_ANCHOR_SPACING_RE = /[ \t]+(<!--\s*id:\s*[A-Za-z0-9_-]+\s*-->)/g
const TRAILING_LINE_WHITESPACE_RE = /[ \t]+$/g

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
  const [structuredBaseContent, setStructuredBaseContent] = useState('')
  const [sectionDrafts, setSectionDrafts] = useState<Record<string, string>>({})
  const [savedSectionDrafts, setSavedSectionDrafts] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const selectedObject = memoryObjects.find((item) => item.id === selectedObjectId)
    ?? buildProfileMemoryObject(workspaceStatus)
  const displaySections = useMemo(
    () => getDisplaySections(selectedObject.kind, sectionDrafts),
    [selectedObject.kind, sectionDrafts],
  )
  const structuredDraftContent = useMemo(
    () => buildStructuredMemoryContent(selectedObject.kind, structuredBaseContent, sectionDrafts),
    [selectedObject.kind, structuredBaseContent, sectionDrafts],
  )
  const currentDraftContent = editorMode === 'structured' ? structuredDraftContent : draftContent
  const currentSaveContent = sanitizeMemoryContent(currentDraftContent)
  const savedSaveContent = sanitizeMemoryContent(savedContent)
  const isDirty = editorMode === 'structured'
    ? !areSectionDraftsEqual(sectionDrafts, savedSectionDrafts) || sanitizeMemoryContent(structuredBaseContent) !== savedSaveContent
    : currentSaveContent !== savedSaveContent

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
    setSectionDrafts((drafts) => ({ ...drafts, [title]: body }))
  }

  const handleEditorModeChange = (mode: EditorMode) => {
    if (mode === editorMode) return
    if (mode === 'markdown') {
      setDraftContent(structuredDraftContent)
    } else {
      setStructuredBaseContent(draftContent)
      setSectionDrafts(buildSectionDrafts(selectedObject.kind, draftContent))
    }
    setEditorMode(mode)
  }

  const handleSave = async (event: FormEvent) => {
    event.preventDefault()
    setSaving(true)
    setNotice(null)
    setError(null)
    try {
      const content = currentSaveContent
      const saved = selectedObject.kind === 'profile'
        ? (await updateProfile(content)).content
        : (await updateSoulMemory(selectedObject.label, content)).content
      setSavedContent(saved)
      setDraftContent(saved)
      setStructuredBaseContent(saved)
      const savedDrafts = buildSectionDrafts(selectedObject.kind, saved)
      setSectionDrafts(savedDrafts)
      setSavedSectionDrafts(savedDrafts)
      setNotice('记忆已保存。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存记忆失败')
    } finally {
      setSaving(false)
    }
  }

  async function loadMemory(object: MemoryObject) {
    setLoading(true)
    setNotice(null)
    setError(null)
    try {
      const content = await fetchContent(object)
      setSavedContent(content)
      setDraftContent(content)
      setStructuredBaseContent(content)
      const drafts = buildSectionDrafts(object.kind, content)
      setSectionDrafts(drafts)
      setSavedSectionDrafts(drafts)
      setEditorMode('structured')
    } catch (err) {
      setSavedContent('')
      setDraftContent('')
      setStructuredBaseContent('')
      setSectionDrafts({})
      setSavedSectionDrafts({})
      setError(err instanceof Error ? err.message : '读取记忆失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className={styles.settingsStack}>
      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}
      {notice && <Notice kind="success" onClose={() => setNotice(null)}>{notice}</Notice>}

      <section className={styles.memoryHero}>
        <div>
          <h2 className={styles.sectionTitle}>记忆工作台</h2>
          <p className={styles.sectionMeta}>用户画像和每个 SOUL 的相处记忆分开管理；人格设定仍留在“人格”页。</p>
        </div>
        <div className={styles.memoryStats}>
          <MemoryStat label="用户画像" value="1" />
          <MemoryStat label="人格记忆" value={souls.length} />
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
                  onClick={() => handleEditorModeChange('structured')}
                >
                  结构化
                </button>
                <button
                  className={`${styles.modeTab} ${editorMode === 'markdown' ? styles.modeTabActive : ''}`}
                  type="button"
                  role="tab"
                  aria-selected={editorMode === 'markdown'}
                  onClick={() => handleEditorModeChange('markdown')}
                >
                  Markdown
                </button>
              </div>
            </div>
          </div>

          <div className={styles.memorySummaryRow}>
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
                ? '这里直接编辑用户画像 Markdown。'
                : '这里只编辑相处记忆，不改变 SOUL 的人格 Markdown。'}
            </span>
            <div className={styles.formActions}>
              <button
                className={workspaceStyles.ghostButton}
                type="button"
                onClick={() => {
                  setDraftContent(savedContent)
                  setStructuredBaseContent(savedContent)
                  setSectionDrafts(savedSectionDrafts)
                }}
                disabled={!isDirty || saving}
              >
                放弃草稿
              </button>
              <button
                className={workspaceStyles.button}
                type="submit"
                disabled={!isDirty || saving || !currentSaveContent.trim()}
              >
                {saving ? '保存中...' : '保存记忆'}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  )
}

function MemoryStat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className={styles.memoryStat}>
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

function getSectionSpecs(kind: MemoryKind): SectionSpec[] {
  return kind === 'profile' ? PROFILE_SECTIONS : SOUL_MEMORY_SECTIONS
}

function buildSectionDrafts(kind: MemoryKind, content: string): Record<string, string> {
  const parsed = parseMarkdownSections(content)
  const sectionMap = new Map(parsed.sections.map((section) => [section.title, stripMemoryAnchors(section.body)]))
  return Object.fromEntries(getSectionSpecs(kind).map((spec) => [spec.title, sectionMap.get(spec.title) ?? '']))
}

function areSectionDraftsEqual(left: Record<string, string>, right: Record<string, string>): boolean {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  for (const key of keys) {
    if ((left[key] ?? '') !== (right[key] ?? '')) return false
  }
  return true
}

function getDisplaySections(kind: MemoryKind, drafts: Record<string, string>) {
  const specs = getSectionSpecs(kind)
  return specs.map((spec) => ({
    ...spec,
    body: drafts[spec.title] ?? '',
  }))
}

function buildStructuredMemoryContent(kind: MemoryKind, baseContent: string, drafts: Record<string, string>): string {
  const specs = getSectionSpecs(kind)
  const editableTitles = new Set(specs.map((spec) => spec.title))
  const parsed = parseMarkdownSections(baseContent)
  const seenTitles = new Set<string>()
  const sections = parsed.sections.map((section) => {
    if (!editableTitles.has(section.title)) return section
    seenTitles.add(section.title)
    return { ...section, body: mergeMemoryAnchors(section.body, drafts[section.title] ?? '') }
  })

  specs.forEach((spec) => {
    if (!seenTitles.has(spec.title)) {
      sections.push({ title: spec.title, body: drafts[spec.title] ?? '' })
    }
  })

  return buildMarkdown(parsed.prefix, sections)
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
      sections.push({ title: currentTitle, body: bodyFromSectionLines(currentBody) })
    }
    currentTitle = title
    currentBody = []
  })

  if (currentTitle !== null) {
    sections.push({ title: currentTitle, body: bodyFromSectionLines(currentBody) })
  }

  return {
    prefix: lines.slice(0, firstSectionIndex).join('\n').trimEnd(),
    sections,
  }
}

function bodyFromSectionLines(lines: string[]): string {
  const bodyLines = [...lines]
  while (bodyLines.length > 0) {
    const lastLine = bodyLines[bodyLines.length - 1]
    if (lastLine === undefined || lastLine.trim()) break
    bodyLines.pop()
  }
  return bodyLines.join('\n')
}

function buildMarkdown(prefix: string, sections: ParsedSection[]): string {
  const parts = []
  const trimmedPrefix = prefix.trimEnd()
  if (trimmedPrefix) {
    parts.push(trimmedPrefix)
  }
  sections.forEach((section) => {
    const body = section.body
    parts.push(body ? `## ${section.title}\n${body}` : `## ${section.title}`)
  })
  return `${parts.join('\n\n')}\n`
}

function sanitizeMemoryContent(content: string): string {
  const sanitized = content
    .split(/\r?\n/)
    .map((line) => line.replace(TRAILING_LINE_WHITESPACE_RE, '').replace(MEMORY_ANCHOR_SPACING_RE, ' $1'))
    .join('\n')
    .trimEnd()
  return sanitized ? `${sanitized}\n` : ''
}

function stripMemoryAnchors(body: string): string {
  return body
    .split(/\r?\n/)
    .map((line) => stripMemoryAnchorFromLine(line))
    .join('\n')
}

function stripMemoryAnchorFromLine(line: string): string {
  return line.replace(MEMORY_ANCHOR_GLOBAL_RE, '')
}

function mergeMemoryAnchors(previousBody: string, nextBody: string): string {
  const previousLines = previousBody.split(/\r?\n/)
  const nextLines = nextBody.split(/\r?\n/)
  const anchorsByVisibleLine = new Map<string, string[]>()
  const usedAnchors = new Set<string>()

  previousLines.forEach((line) => {
    const anchor = extractMemoryAnchor(line)
    if (!anchor) return
    const visibleLine = normalizeMemoryLine(line)
    if (!visibleLine) return
    const anchors = anchorsByVisibleLine.get(visibleLine) ?? []
    anchors.push(anchor)
    anchorsByVisibleLine.set(visibleLine, anchors)
  })

  const mergedAnchors = nextLines.map((line) => {
    if (extractMemoryAnchor(line)) return null
    const visibleLine = normalizeMemoryLine(line)
    const exactMatch = visibleLine ? anchorsByVisibleLine.get(visibleLine)?.find((anchor) => !usedAnchors.has(anchor)) : undefined
    if (!exactMatch) return null
    usedAnchors.add(exactMatch)
    return exactMatch
  })

  nextLines.forEach((line, index) => {
    if (mergedAnchors[index] !== null || extractMemoryAnchor(line)) return
    if (!normalizeMemoryLine(line)) return
    const previousAnchor = extractMemoryAnchor(previousLines[index] ?? '')
    if (!previousAnchor || usedAnchors.has(previousAnchor)) return
    mergedAnchors[index] = previousAnchor
    usedAnchors.add(previousAnchor)
  })

  return nextLines
    .map((line, index) => {
      const existingAnchor = extractMemoryAnchor(line)
      const anchor = existingAnchor ?? mergedAnchors[index]
      if (!anchor) return stripMemoryAnchorFromLine(line)
      const visibleLine = stripMemoryAnchorFromLine(line)
      if (!visibleLine.trim()) return visibleLine
      return `${visibleLine} ${anchor}`
    })
    .join('\n')
}

function extractMemoryAnchor(line: string): string | null {
  return MEMORY_ANCHOR_RE.exec(line)?.[0] ?? null
}

function normalizeMemoryLine(line: string): string {
  return stripMemoryAnchorFromLine(line).trim()
}

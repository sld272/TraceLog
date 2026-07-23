import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import {
  type ModelSettings,
  type ModelSettingsUpdate,
  type LogStats,
  type ScheduleClientIdInfo,
  type ScheduleStatus,
  type Soul,
  type WorkspaceStatus,
  ApiError,
  fetchHealth,
  createSoul,
  clearLogFiles,
  generateSoul,
  getAuthStatus,
  getLogStats,
  getModelSettings,
  getScheduleClientId,
  getScheduleStatus,
  getSoulContent,
  getWorkspaceStatus,
  listSouls,
  reconcileVectorIndex,
  revealLogFolder,
  reorderSouls,
  restoreDefaultClientId,
  retryVectorIndex,
  saveModelSettings,
  saveScheduleClientId,
  createLocalCalendarAccount,
  deleteLocalCalendarAccount,
  scheduleLogout,
  startInteractiveAuth,
  startScheduleDeviceLogin,
  updateSoul,
} from '@/api/client'
import { formatSmartTime } from '@/utils/date'
import { invalidateScheduleStatusCache, setCachedScheduleStatus } from '@/utils/scheduleStatusCache'
import { Notice } from '@/components/Notice'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { ScheduleMigrationDialog } from '@/components/ScheduleMigrationDialog'
import { ArrowDownIcon, ArrowUpIcon, ChevronRightIcon, PencilIcon } from '@/components/icons'
import { SoulAvatar } from '@/components/SoulAvatar'
import workspaceStyles from './WorkspacePages.module.css'
import styles from './SettingsPage.module.css'

type SettingsTab = 'model' | 'souls' | 'schedule' | 'data' | 'about'
type CreateSoulMode = 'ai' | 'markdown'
type WebSearchProvider = ModelSettings['web_search']['provider']

interface AiSoulDraft {
  name: string
  inspiration: string
}

interface MarkdownSoulDraft {
  name: string
  content: string
}

interface SoulPreview {
  name: string
  inspiration: string
  content: string
  searchUsed: boolean
  sources: { title: string; url: string }[]
}

interface SoulEditing {
  name: string
  content: string
  original: string
}

interface SettingsPageProps {
  firstRun?: boolean
  initialTab?: 'schedule'
  onModelSettingsChanged?: () => void
  onSoulsChanged?: () => void
}

interface ModelForm {
  api_key: string
  base_url: string
  model: string
  embedding_model: string
  embedding_api_key: string
  embedding_base_url: string
  reuse_embedding_config: boolean
  secondary_model: string
  secondary_api_key: string
  secondary_base_url: string
  reuse_secondary_config: boolean
  reuse_secondary_api_key: boolean
  logging: ModelSettings['logging']
  vision: {
    enabled: boolean
    model: string
    api_key: string
    base_url: string
  }
  web_search: {
    enabled: boolean
    provider: WebSearchProvider
    tavily_api_key: string
    max_results: number
    timeout_s: number
    cache_ttl_s: number
  }
}

const DEFAULT_MODEL_FORM: ModelForm = {
  api_key: '',
  base_url: 'https://api.openai.com/v1',
  model: 'gpt-4o-mini',
  embedding_model: 'text-embedding-3-small',
  embedding_api_key: '',
  embedding_base_url: '',
  reuse_embedding_config: true,
  secondary_model: '',
  secondary_api_key: '',
  secondary_base_url: '',
  reuse_secondary_config: true,
  reuse_secondary_api_key: true,
  logging: {
    enabled: true,
    level: 'INFO',
    capture_content: false,
    rotate_max_bytes: 10 * 1024 * 1024,
    history_max_bytes: 50 * 1024 * 1024,
    history_max_days: 14,
  },
  vision: {
    enabled: false,
    model: '',
    api_key: '',
    base_url: '',
  },
  web_search: {
    enabled: false,
    provider: 'duckduckgo',
    tavily_api_key: '',
    max_results: 5,
    timeout_s: 8,
    cache_ttl_s: 1800,
  },
}

const AI_SOUL_PLACEHOLDER = '写下你想要的人格。可以描述性格、语气、相处方式、边界、适合的场景，或任何灵感。系统会把它整理成完整的人格 Markdown 文件。'

const TAB_SUBTITLES: Record<SettingsTab, string> = {
  model: '模型、图片识别、网页搜索与 Embedding 配置',
  souls: '排序决定首页并发回应顺序，禁用后不进入回应队列',
  schedule: '管理日历账号：Outlook 云端与本地日历',
  data: '本地 workspace 状态、数据概览与记忆检索索引',
  about: '关于拾迹这个项目',
}

export function SettingsPage({ firstRun = false, initialTab, onModelSettingsChanged, onSoulsChanged }: SettingsPageProps) {
  const [activeTab, setActiveTab] = useState<SettingsTab>(initialTab ?? 'model')
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null)
  const [modelForm, setModelForm] = useState<ModelForm>(DEFAULT_MODEL_FORM)
  const [souls, setSouls] = useState<Soul[]>([])
  const [workspaceStatus, setWorkspaceStatus] = useState<WorkspaceStatus | null>(null)
  const [logStats, setLogStats] = useState<LogStats | null>(null)
  const [createSoulMode, setCreateSoulMode] = useState<CreateSoulMode>('ai')
  const [aiSoulDraft, setAiSoulDraft] = useState<AiSoulDraft>({ name: '', inspiration: '' })
  const [markdownSoulDraft, setMarkdownSoulDraft] = useState<MarkdownSoulDraft>({ name: '', content: '' })
  const [soulPreview, setSoulPreview] = useState<SoulPreview | null>(null)
  const [previewFeedback, setPreviewFeedback] = useState('')
  const [editingSoul, setEditingSoul] = useState<SoulEditing | null>(null)
  const [editFeedback, setEditFeedback] = useState('')
  const [loading, setLoading] = useState(true)
  const [savingModel, setSavingModel] = useState(false)
  const [savingSoul, setSavingSoul] = useState<string | null>(null)
  const [vectorAction, setVectorAction] = useState<'retry' | 'reconcile' | null>(null)
  const [vectorError, setVectorError] = useState<string | null>(null)
  const [logAction, setLogAction] = useState<'toggle' | 'clear' | 'reveal' | null>(null)
  const [confirmClearLogs, setConfirmClearLogs] = useState(false)
  const [logRevealPath, setLogRevealPath] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setActiveTab(initialTab ?? 'model')
  }, [initialTab])

  const tabs = useMemo(
    () => [
      { id: 'model' as const, label: '基本' },
      { id: 'souls' as const, label: '人格' },
      { id: 'schedule' as const, label: '日程' },
      { id: 'data' as const, label: '数据' },
      { id: 'about' as const, label: '关于' },
    ],
    [],
  )

  useEffect(() => {
    refreshSettings()
  }, [])

  const refreshSettings = async () => {
    try {
      setLoading(true)
      const [model, soulList, workspace, logs] = await Promise.all([
        getModelSettings(),
        listSouls(false),
        getWorkspaceStatus(),
        getLogStats(),
      ])
      setModelSettings(model)
      setModelForm(formFromModelSettings(model))
      setSouls(soulList)
      setWorkspaceStatus(workspace)
      setLogStats(logs)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载设置失败')
    } finally {
      setLoading(false)
    }
  }

  const handleSaveModel = async (event: FormEvent) => {
    event.preventDefault()
    setSavingModel(true)
    setNotice(null)
    setError(null)
    try {
      const saved = await saveModelSettings(toModelUpdate(modelForm))
      setModelSettings(saved)
      setModelForm(formFromModelSettings(saved))
      onModelSettingsChanged?.()
      if (saved.config_reloaded ?? saved.runtime_reloaded) {
        setNotice('配置已保存，应用已重新加载配置。')
      } else if (saved.reload_error) {
        setNotice(`配置已保存，但重新加载失败：${saved.reload_error}。当前仍在使用上一次可用配置。`)
      } else {
        setNotice('配置已保存。')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存模型配置失败')
    } finally {
      setSavingModel(false)
    }
  }

  const handleCaptureContentChange = async (captureContent: boolean) => {
    const savedForm = modelSettings ? formFromModelSettings(modelSettings) : modelForm
    const nextForm: ModelForm = {
      ...savedForm,
      logging: { ...savedForm.logging, capture_content: captureContent },
    }
    setLogAction('toggle')
    setNotice(null)
    setError(null)
    try {
      const saved = await saveModelSettings(toModelUpdate(nextForm))
      setModelSettings(saved)
      setModelForm((current) => ({ ...current, logging: saved.logging }))
      setLogStats(await getLogStats())
      onModelSettingsChanged?.()
      setNotice('调试日志设置已保存。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存调试日志设置失败')
    } finally {
      setLogAction(null)
    }
  }

  const handleClearLogs = async () => {
    setLogAction('clear')
    setNotice(null)
    setError(null)
    try {
      setLogStats(await clearLogFiles())
      setConfirmClearLogs(false)
      setNotice('调试日志已清空。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '清空调试日志失败')
    } finally {
      setLogAction(null)
    }
  }

  const handleRevealLogs = async () => {
    setLogAction('reveal')
    setLogRevealPath(null)
    setError(null)
    try {
      const result = await revealLogFolder()
      if (!result.ok) setLogRevealPath(result.path)
    } catch (err) {
      setError(err instanceof Error ? err.message : '打开日志文件夹失败')
    } finally {
      setLogAction(null)
    }
  }

  const handleToggleSoul = async (soul: Soul) => {
    setSavingSoul(soul.name)
    setNotice(null)
    setError(null)
    try {
      const updated = await updateSoul(soul.name, { enabled: !soul.enabled })
      setSouls((items) => items.map((item) => item.name === soul.name ? updated : item))
      onSoulsChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新人格失败')
    } finally {
      setSavingSoul(null)
    }
  }

  const handleMoveSoul = async (index: number, direction: -1 | 1) => {
    const target = index + direction
    if (target < 0 || target >= souls.length) return
    const next = [...souls]
    const [moved] = next.splice(index, 1)
    if (!moved) return
    next.splice(target, 0, moved)

    setSavingSoul(moved.name)
    setNotice(null)
    setError(null)
    try {
      const response = await reorderSouls(next.map((soul) => soul.name))
      setSouls(response.souls)
      onSoulsChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : '调整人格排序失败')
    } finally {
      setSavingSoul(null)
    }
  }

  const handleCreateSoul = async (event: FormEvent) => {
    event.preventDefault()
    const draft = createSoulMode === 'ai' ? aiSoulDraft : markdownSoulDraft
    const name = draft.name.trim()
    const content = (createSoulMode === 'ai' ? aiSoulDraft.inspiration : markdownSoulDraft.content).trim()
    if (!name || !content) return
    if (createSoulMode === 'ai') {
      // AI 模式：先生成预览，用户确认后才真正创建
      setSavingSoul('generate')
      setNotice(null)
      setError(null)
      try {
        const result = await generateSoul(name, content)
        setSoulPreview({
          name,
          inspiration: content,
          content: result.soul,
          searchUsed: result.search_used,
          sources: result.sources ?? [],
        })
        setPreviewFeedback('')
      } catch (err) {
        setError(err instanceof Error ? err.message : '生成人格失败')
      } finally {
        setSavingSoul(null)
      }
      return
    }
    setSavingSoul('new')
    setNotice(null)
    setError(null)
    try {
      const created = await createSoul(name, null, true, content)
      setSouls((items) => [...items, created].sort((a, b) => a.sort_order - b.sort_order))
      setMarkdownSoulDraft({ name: '', content: '' })
      setNotice(`已创建 ${created.name}`)
      onSoulsChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建人格失败')
    } finally {
      setSavingSoul(null)
    }
  }

  const handleRegeneratePreview = async () => {
    if (!soulPreview) return
    setSavingSoul('generate')
    setNotice(null)
    setError(null)
    try {
      const result = await generateSoul(soulPreview.name, soulPreview.inspiration)
      setSoulPreview({
        ...soulPreview,
        content: result.soul,
        searchUsed: result.search_used,
        sources: result.sources ?? [],
      })
      setPreviewFeedback('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '重新生成失败')
    } finally {
      setSavingSoul(null)
    }
  }

  const handleRefinePreview = async () => {
    if (!soulPreview) return
    const feedback = previewFeedback.trim()
    if (!feedback || !soulPreview.content.trim()) return
    setSavingSoul('refine')
    setNotice(null)
    setError(null)
    try {
      const result = await generateSoul(soulPreview.name, soulPreview.inspiration, {
        currentSoul: soulPreview.content,
        feedback,
      })
      // 修订轮不重新搜索，保留首轮的参考来源展示
      setSoulPreview({ ...soulPreview, content: result.soul })
      setPreviewFeedback('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'AI 调整失败')
    } finally {
      setSavingSoul(null)
    }
  }

  const handlePreviewContentChange = (content: string) => {
    setSoulPreview((preview) => (preview ? { ...preview, content } : preview))
  }

  const handleConfirmCreateSoul = async () => {
    if (!soulPreview || !soulPreview.content.trim()) return
    setSavingSoul('new')
    setNotice(null)
    setError(null)
    try {
      const created = await createSoul(soulPreview.name, null, true, soulPreview.content)
      setSouls((items) => [...items, created].sort((a, b) => a.sort_order - b.sort_order))
      setSoulPreview(null)
      setPreviewFeedback('')
      setAiSoulDraft({ name: '', inspiration: '' })
      setNotice(`已创建 ${created.name}`)
      onSoulsChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建人格失败')
    } finally {
      setSavingSoul(null)
    }
  }

  const handleDiscardPreview = () => {
    setSoulPreview(null)
    setPreviewFeedback('')
  }

  const handleStartEditSoul = async (soul: Soul) => {
    setSavingSoul('edit-load')
    setNotice(null)
    setError(null)
    try {
      const { soul: content } = await getSoulContent(soul.name)
      setEditingSoul({ name: soul.name, content, original: content })
      setEditFeedback('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取人格文件失败')
    } finally {
      setSavingSoul(null)
    }
  }

  const handleEditContentChange = (content: string) => {
    setEditingSoul((editing) => (editing ? { ...editing, content } : editing))
  }

  const handleRefineEditingSoul = async () => {
    if (!editingSoul) return
    const feedback = editFeedback.trim()
    if (!feedback || !editingSoul.content.trim()) return
    setSavingSoul('edit-refine')
    setNotice(null)
    setError(null)
    try {
      const result = await generateSoul(editingSoul.name, feedback, {
        currentSoul: editingSoul.content,
        feedback,
      })
      setEditingSoul({ ...editingSoul, content: result.soul })
      setEditFeedback('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'AI 调整失败')
    } finally {
      setSavingSoul(null)
    }
  }

  const handleSaveEditingSoul = async () => {
    if (!editingSoul || !editingSoul.content.trim()) return
    setSavingSoul('edit-save')
    setNotice(null)
    setError(null)
    try {
      const updated = await updateSoul(editingSoul.name, { soul: editingSoul.content })
      setSouls((items) => items.map((item) => (item.name === updated.name ? updated : item)))
      setEditingSoul(null)
      setEditFeedback('')
      setNotice(`已保存 ${updated.name}`)
      onSoulsChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存人格失败')
    } finally {
      setSavingSoul(null)
    }
  }

  const handleCancelEditSoul = () => {
    if (
      editingSoul &&
      editingSoul.content !== editingSoul.original &&
      !window.confirm('有未保存的修改，确定放弃吗？')
    ) {
      return
    }
    setEditingSoul(null)
    setEditFeedback('')
  }

  const handleCreateSoulModeChange = (mode: CreateSoulMode) => {
    setCreateSoulMode(mode)
    if (mode === 'markdown' && !markdownSoulDraft.content.trim()) {
      setMarkdownSoulDraft((draft) => ({
        ...draft,
        content: newSoulMarkdownTemplate(draft.name),
      }))
    }
  }

  const handleNewSoulNameChange = (name: string) => {
    if (createSoulMode === 'ai') {
      setAiSoulDraft((draft) => ({ ...draft, name }))
      return
    }
    setMarkdownSoulDraft((draft) => ({
      name,
      content: isDefaultSoulTemplate(draft.content, draft.name)
        ? newSoulMarkdownTemplate(name)
        : draft.content,
    }))
  }

  const handleNewSoulContentChange = (content: string) => {
    if (createSoulMode === 'ai') {
      setAiSoulDraft((draft) => ({ ...draft, inspiration: content }))
      return
    }
    setMarkdownSoulDraft((draft) => ({ ...draft, content }))
  }

  const handleVectorAction = async (action: 'retry' | 'reconcile') => {
    setVectorAction(action)
    setNotice(null)
    setVectorError(null)
    setError(null)
    try {
      const result = action === 'retry'
        ? await retryVectorIndex()
        : await reconcileVectorIndex()
      const workspace = await getWorkspaceStatus()
      setWorkspaceStatus(workspace)
      setNotice(`向量索引已处理 ${result.processed} 条任务。`)
    } catch (err) {
      setVectorError(err instanceof Error ? err.message : '记忆检索失败')
    } finally {
      setVectorAction(null)
    }
  }

  return (
    <div className={workspaceStyles.page}>
      <header className={workspaceStyles.header}>
        <div className={workspaceStyles.titleGroup}>
          <h1 className={workspaceStyles.title}>设置</h1>
          <p className={workspaceStyles.subtitle}>{TAB_SUBTITLES[activeTab]}</p>
        </div>
      </header>

      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}
      {firstRun && (
        <Notice kind="info">首次使用 TraceLog，请先配置主模型和 Embedding。保存后应用会自动重新加载配置。</Notice>
      )}
      {notice && <Notice kind="success" onClose={() => setNotice(null)}>{notice}</Notice>}

      <div className={styles.settingsGrid}>
        <nav className={styles.settingsSide} role="tablist" aria-label="设置分类">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              className={`${styles.sideItem} ${activeTab === tab.id ? styles.sideItemActive : ''}`}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              role="tab"
              aria-selected={activeTab === tab.id}
            >
              {tab.label}
            </button>
          ))}
        </nav>

        <div className={styles.settingsMain}>
          {loading ? (
            <div className={workspaceStyles.empty}>加载设置中...</div>
          ) : (
            <>
              {activeTab === 'model' && (
                <ModelSettingsPanel
                  form={modelForm}
                  settings={modelSettings}
                  saving={savingModel}
                  onChange={setModelForm}
                  onSubmit={handleSaveModel}
                />
              )}
              {activeTab === 'souls' && (
                <SoulSettingsPanel
                  souls={souls}
                  savingSoul={savingSoul}
                  createSoulMode={createSoulMode}
                  newSoulName={createSoulMode === 'ai' ? aiSoulDraft.name : markdownSoulDraft.name}
                  newSoulContent={createSoulMode === 'ai' ? aiSoulDraft.inspiration : markdownSoulDraft.content}
                  preview={soulPreview}
                  previewFeedback={previewFeedback}
                  editing={editingSoul}
                  editFeedback={editFeedback}
                  onCreateSoulModeChange={handleCreateSoulModeChange}
                  onNewSoulNameChange={handleNewSoulNameChange}
                  onNewSoulContentChange={handleNewSoulContentChange}
                  onCreateSoul={handleCreateSoul}
                  onToggleSoul={handleToggleSoul}
                  onMoveSoul={handleMoveSoul}
                  onPreviewContentChange={handlePreviewContentChange}
                  onPreviewFeedbackChange={setPreviewFeedback}
                  onRegeneratePreview={handleRegeneratePreview}
                  onRefinePreview={handleRefinePreview}
                  onConfirmCreateSoul={handleConfirmCreateSoul}
                  onDiscardPreview={handleDiscardPreview}
                  onStartEditSoul={handleStartEditSoul}
                  onEditContentChange={handleEditContentChange}
                  onEditFeedbackChange={setEditFeedback}
                  onRefineEditingSoul={handleRefineEditingSoul}
                  onSaveEditingSoul={handleSaveEditingSoul}
                  onCancelEditSoul={handleCancelEditSoul}
                />
              )}
              {activeTab === 'schedule' && <ScheduleSettingsPanel />}
              {activeTab === 'data' && (
                <DataSettingsPanel
                  status={workspaceStatus}
                  logStats={logStats}
                  logAction={logAction}
                  confirmClearLogs={confirmClearLogs}
                  logRevealPath={logRevealPath}
                  vectorAction={vectorAction}
                  vectorError={vectorError}
                  onVectorAction={handleVectorAction}
                  onCaptureContentChange={handleCaptureContentChange}
                  onRequestClearLogs={() => setConfirmClearLogs(true)}
                  onCancelClearLogs={() => setConfirmClearLogs(false)}
                  onClearLogs={handleClearLogs}
                  onRevealLogs={handleRevealLogs}
                />
              )}
              {activeTab === 'about' && <AboutSettingsPanel />}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

const RELEASES_URL = 'https://github.com/sld272/TraceLog/releases'

/** 比较 'v1.2.3' 风格版本号；返回正数表示 a 更新。 */
function compareVersions(a: string, b: string): number {
  const parse = (value: string) => value.replace(/^v/i, '').split('.').map((n) => parseInt(n, 10) || 0)
  const pa = parse(a)
  const pb = parse(b)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0)
    if (diff !== 0) return diff
  }
  return 0
}

type UpdateCheck =
  | { state: 'idle' }
  | { state: 'checking' }
  | { state: 'latest' }
  | { state: 'update'; tag: string; url: string }
  | { state: 'error' }

function AboutSettingsPanel() {
  const [version, setVersion] = useState<string | null>(null)
  const [update, setUpdate] = useState<UpdateCheck>({ state: 'idle' })

  useEffect(() => {
    let cancelled = false
    fetchHealth()
      .then((health) => {
        if (!cancelled) setVersion(health.version)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])

  const checkForUpdates = async () => {
    setUpdate({ state: 'checking' })
    try {
      const res = await fetch('https://api.github.com/repos/sld272/TraceLog/releases/latest', {
        headers: { Accept: 'application/vnd.github+json' },
      })
      if (res.status === 404) {
        setUpdate({ state: 'latest' })
        return
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const release = await res.json()
      const tag = String(release.tag_name ?? '')
      if (version && tag && compareVersions(tag, version) > 0) {
        setUpdate({ state: 'update', tag, url: String(release.html_url ?? RELEASES_URL) })
      } else {
        setUpdate({ state: 'latest' })
      }
    } catch {
      setUpdate({ state: 'error' })
    }
  }

  return (
    <div className={styles.aboutPage}>
      <img className={styles.apIcon} src="/brand/tracelog-icon-transparent-512.png" alt="拾迹" />
      <div className={styles.apWords}>
        <img className={styles.apShiji} src="/brand/shiji-wordmark-transparent.png" alt="拾迹" />
        <img className={styles.apWord} src="/brand/tracelog-wordmark-transparent.png" alt="TraceLog" />
      </div>
      <div className={styles.apCn}>向内运行的 AI 社交媒体，也是一台陪你成长的记忆引擎。</div>
      <p className={styles.apLead}>
        在这里，你和 AI 好友的每段对话都不会白费——它们会沉淀成记忆，看见你的成长轨迹。
      </p>
      <p className={styles.apDesc}>
        拾迹把「社交媒体的表达」和「AI 的长期记忆」缝在一起。你发帖、和不同性格的 AI 好友聊天，系统会在背后整理、提炼出关于你的画像与记忆条目，并保留每一条记忆的证据来源。记忆不是黑盒——画像、记忆条目、证据追溯、编辑与删除，都在记忆工作台里一目了然。
      </p>
      <div className={styles.aboutFeats}>
        <span className={styles.ft}>成长记忆引擎</span>
        <span className={styles.ft}>多重 AI 人格</span>
        <span className={styles.ft}>证据可追溯</span>
        <span className={styles.ft}>目标与待办</span>
        <span className={styles.ft}>本地优先</span>
      </div>
      <div className={styles.aboutMeta}>
        <span>{version ? `版本 v${version}` : '版本 —'}</span>
        <button
          type="button"
          className={styles.apUpdateCheck}
          onClick={checkForUpdates}
          disabled={update.state === 'checking'}
        >
          {update.state === 'checking' ? '检查中…' : '检查更新'}
        </button>
        {update.state === 'latest' && <span>已是最新版本</span>}
        {update.state === 'update' && (
          <a href={update.url} target="_blank" rel="noreferrer">
            发现新版本 {update.tag}，前往下载
          </a>
        )}
        {update.state === 'error' && (
          <a href={RELEASES_URL} target="_blank" rel="noreferrer">
            检查失败，去发布页看看
          </a>
        )}
        <a href="https://github.com/sld272/TraceLog" target="_blank" rel="noreferrer">
          GitHub 仓库
        </a>
      </div>
    </div>
  )
}

type ScheduleBusy = 'login' | 'device' | 'save' | 'restore' | 'logout' | 'createLocal' | 'deleteLocal' | null
type LoginPhase = 'interactive' | 'device' | null

function ScheduleSettingsPanel() {
  const [status, setStatus] = useState<ScheduleStatus | null>(null)
  const [clientIdInfo, setClientIdInfo] = useState<ScheduleClientIdInfo | null>(null)
  const [clientIdInput, setClientIdInput] = useState('')
  const [device, setDevice] = useState<{ user_code: string; verification_uri: string } | null>(null)
  const [busy, setBusy] = useState<ScheduleBusy>(null)
  const [loginPhase, setLoginPhase] = useState<LoginPhase>(null)
  const [polling, setPolling] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [confirmRestore, setConfirmRestore] = useState(false)
  const [confirmDeleteLocal, setConfirmDeleteLocal] = useState(false)
  const [showMigration, setShowMigration] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  const stopPolling = () => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
    setPolling(false)
  }

  // 两个请求各自独立落地：client-id 几乎瞬时（决定内置/自定义应用标签），
  // status 可能较慢（首次需要唤起 Graph/MSAL），互不阻塞。
  const reload = async () => {
    await Promise.allSettled([
      getScheduleStatus()
        .then((data) => {
          setStatus(data)
          setCachedScheduleStatus(data)
        })
        .catch(() => {}),
      getScheduleClientId().then(setClientIdInfo).catch(() => {}),
    ])
  }

  useEffect(() => {
    void reload()
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current)
    }
  }, [])

  // 轮询统一的登录状态（interactive 与 device 共用后端 _auth_state）。
  // interactive 失败且 fallback==='device_code' 时自动切入设备码流。
  const startPolling = (phase: 'interactive' | 'device') => {
    setPolling(true)
    pollRef.current = window.setInterval(() => {
      void getAuthStatus()
        .then(async (result) => {
          if (result.status === 'ok') {
            stopPolling()
            setDevice(null)
            setLoginPhase(null)
            setNotice('已连接 Microsoft 账户。')
            await reload()
          } else if (result.status === 'error') {
            stopPolling()
            if (phase === 'interactive' && result.fallback === 'device_code') {
              await beginDeviceFlow()
            } else {
              setDevice(null)
              setLoginPhase(null)
              setError(result.error || 'Microsoft 登录失败')
            }
          }
        })
        .catch(() => {
          /* 轮询期间的瞬时错误忽略，继续等待 */
        })
    }, 2500)
  }

  const beginDeviceFlow = async () => {
    setError(null)
    setLoginPhase('device')
    try {
      const flow = await startScheduleDeviceLogin()
      setDevice({ user_code: flow.user_code, verification_uri: flow.verification_uri })
      startPolling('device')
    } catch (err) {
      setLoginPhase(null)
      if (err instanceof ApiError && err.status === 409) {
        setError('Microsoft 登录正在进行中，请稍候重试。')
      } else {
        setError(err instanceof Error ? err.message : '启动设备码登录失败')
      }
    }
  }

  const handleInteractiveLogin = async () => {
    setBusy('login')
    setError(null)
    setNotice(null)
    setDevice(null)
    setLoginPhase('interactive')
    try {
      await startInteractiveAuth()
      startPolling('interactive')
    } catch (err) {
      setLoginPhase(null)
      if (err instanceof ApiError && err.status === 409) {
        setError('Microsoft 登录正在进行中，请稍候重试。')
      } else {
        setError(err instanceof Error ? err.message : '启动登录失败')
      }
    } finally {
      setBusy(null)
    }
  }

  const handleDeviceLogin = async () => {
    setBusy('device')
    setNotice(null)
    setDevice(null)
    await beginDeviceFlow()
    setBusy(null)
  }

  const handleSaveClientId = async () => {
    const value = clientIdInput.trim()
    if (!value) {
      setError('client_id 不能为空')
      return
    }
    setBusy('save')
    setError(null)
    setNotice(null)
    stopPolling()
    setDevice(null)
    setLoginPhase(null)
    try {
      const info = await saveScheduleClientId(value)
      invalidateScheduleStatusCache()
      setClientIdInfo(info)
      setClientIdInput('')
      setNotice('已保存自定义应用 ID，请重新登录。')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存 client_id 失败')
    } finally {
      setBusy(null)
    }
  }

  const handleRestoreDefault = async () => {
    setConfirmRestore(false)
    setBusy('restore')
    setError(null)
    setNotice(null)
    stopPolling()
    setDevice(null)
    setLoginPhase(null)
    try {
      const info = await restoreDefaultClientId()
      invalidateScheduleStatusCache()
      setClientIdInfo(info)
      setClientIdInput('')
      setNotice('已恢复内置应用，请重新登录。')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复内置应用失败')
    } finally {
      setBusy(null)
    }
  }

  const handleLogout = async () => {
    setBusy('logout')
    setError(null)
    setNotice(null)
    stopPolling()
    setDevice(null)
    setLoginPhase(null)
    try {
      await scheduleLogout()
      invalidateScheduleStatusCache()
      setNotice('已退出 Microsoft 登录。目标关联会在重新登录同一账户后自动恢复。')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : '退出登录失败')
    } finally {
      setBusy(null)
    }
  }

  const copy = (text: string) => {
    void navigator.clipboard?.writeText(text).then(
      () => setNotice('已复制到剪贴板。'),
      () => undefined,
    )
  }

  const localAccount = status?.accounts?.find((account) => account.provider === 'local') ?? null

  const handleCreateLocal = async () => {
    setBusy('createLocal')
    setError(null)
    setNotice(null)
    try {
      await createLocalCalendarAccount()
      invalidateScheduleStatusCache()
      setNotice('已创建本地日历。日程仅保存在这台设备。')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建本地日历失败')
    } finally {
      setBusy(null)
    }
  }

  const handleDeleteLocal = async () => {
    if (!confirmDeleteLocal) {
      setConfirmDeleteLocal(true)
      return
    }
    setBusy('deleteLocal')
    setError(null)
    setNotice(null)
    try {
      const result = await deleteLocalCalendarAccount(true)
      invalidateScheduleStatusCache()
      setConfirmDeleteLocal(false)
      setNotice(`已删除本地日历（连同 ${result.deleted_events} 条日程）。`)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除本地日历失败')
    } finally {
      setBusy(null)
    }
  }

  const connected = status?.connected ?? false
  const accountName = status?.account?.username ?? status?.account?.name ?? null
  const usingDefault = clientIdInfo?.using_default ?? true
  const clientTail = clientIdInfo?.client_id_tail ?? ''
  const advancedState = usingDefault
    ? `使用拾迹内置应用（···${clientTail}）`
    : `自定义应用（···${clientTail}）`
  // 登录进行中（轮询、已弹出设备码或等待浏览器授权）时锁住入口。
  const loginInFlight = polling || loginPhase !== null

  return (
    <div className={styles.settingsStack}>
      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}
      {notice && <Notice kind="success" onClose={() => setNotice(null)}>{notice}</Notice>}

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>Outlook 日历</h2>
            <p className={styles.sectionMeta}>登录 Microsoft 后日程多端同步，推荐使用。</p>
          </div>
          <StatusPill ok={connected} label={connected ? '已连接' : '未连接'} />
        </div>
        {connected ? (
          <div className={styles.sectionBodyStack}>
            <p className={styles.sectionMeta}>
              当前账户：{accountName || '（未知）'}
              {status?.last_sync_at ? ` · 上次同步 ${formatSmartTime(status.last_sync_at)}` : ''}
            </p>
            <div className={styles.actionRow}>
              <button
                className={workspaceStyles.dangerButton}
                type="button"
                onClick={() => void handleLogout()}
                disabled={busy !== null}
              >
                {busy === 'logout' ? '退出中...' : '退出登录'}
              </button>
            </div>
          </div>
        ) : device ? (
          <div className={styles.deviceBox}>
            <p className={styles.deviceHint}>
              在浏览器打开下面的地址，输入验证码完成登录。登录成功后本页会自动刷新。
            </p>
            <div className={styles.deviceRow}>
              <span className={styles.deviceLabel}>验证码</span>
              <code className={styles.deviceCode}>{device.user_code}</code>
              <button className={styles.copyBtn} type="button" onClick={() => copy(device.user_code)}>复制</button>
            </div>
            <div className={styles.deviceRow}>
              <span className={styles.deviceLabel}>登录地址</span>
              <a className={styles.deviceUrl} href={device.verification_uri} target="_blank" rel="noreferrer">
                {device.verification_uri}
              </a>
              <button className={styles.copyBtn} type="button" onClick={() => copy(device.verification_uri)}>复制</button>
            </div>
            <p className={styles.deviceStatus}>{polling ? '正在等待你完成登录…' : '登录会话已结束。'}</p>
          </div>
        ) : loginPhase === 'interactive' ? (
          <div className={styles.deviceBox}>
            <p className={styles.deviceHint}>
              浏览器将弹出微软登录页，完成授权后会自动返回。若没有弹出窗口，会自动切换到设备码登录。
            </p>
            <p className={styles.deviceStatus}>正在等待浏览器授权…</p>
          </div>
        ) : (
          <div className={styles.connectStack}>
            <button
              className={workspaceStyles.button}
              type="button"
              onClick={() => void handleInteractiveLogin()}
              disabled={busy !== null || loginInFlight}
            >
              {busy === 'login' ? '启动中...' : '登录 Microsoft'}
            </button>
            <button
              className={styles.textLink}
              type="button"
              onClick={() => void handleDeviceLogin()}
              disabled={busy !== null || loginInFlight}
            >
              改用设备码登录
            </button>
          </div>
        )}
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>本地日历</h2>
            <p className={styles.sectionMeta}>日程仅保存在这台设备，不同步到任何云端。</p>
          </div>
          {localAccount && <StatusPill ok label="已启用" />}
        </div>
        {localAccount ? (
          <div className={styles.sectionBodyStack}>
            <p className={styles.sectionMeta}>共 {localAccount.event_count} 条本地日程。</p>
            <div className={styles.actionRow}>
              {connected && localAccount.event_count > 0 && !confirmDeleteLocal && (
                <button
                  className={workspaceStyles.ghostButton}
                  type="button"
                  onClick={() => setShowMigration(true)}
                  disabled={busy !== null}
                >
                  迁入 Outlook
                </button>
              )}
              <button
                className={workspaceStyles.dangerButton}
                type="button"
                onClick={() => void handleDeleteLocal()}
                disabled={busy !== null}
              >
                {busy === 'deleteLocal'
                  ? '删除中...'
                  : confirmDeleteLocal
                    ? `确认删除？将连同 ${localAccount.event_count} 条日程一并删除，不可恢复`
                    : '删除本地日历'}
              </button>
              {confirmDeleteLocal && busy === null && (
                <button
                  className={workspaceStyles.ghostButton}
                  type="button"
                  onClick={() => setConfirmDeleteLocal(false)}
                >
                  取消
                </button>
              )}
            </div>
          </div>
        ) : (
          <div className={styles.actionRow}>
            <button
              className={workspaceStyles.ghostButton}
              type="button"
              onClick={() => void handleCreateLocal()}
              disabled={busy !== null}
            >
              {busy === 'createLocal' ? '创建中...' : '创建本地日历'}
            </button>
          </div>
        )}
      </section>

      <p className={styles.scheduleNote}>
        默认使用拾迹内置应用，无需注册；自托管或企业环境可在高级选项中改用自己的应用 ID。
      </p>

      <section className={styles.section}>
        <button
          className={styles.advancedHeader}
          type="button"
          onClick={() => setAdvancedOpen((value) => !value)}
          aria-expanded={advancedOpen}
        >
          <span className={styles.advancedChevron} data-open={advancedOpen}>
            <ChevronRightIcon width={16} height={16} />
          </span>
          <span className={styles.advancedTitle}>高级选项</span>
          <span className={styles.advancedState}>{advancedState}</span>
        </button>
        {advancedOpen && (
          <div className={styles.advancedBody}>
            <div>
              <h3 className={styles.advancedSubTitle}>Microsoft 应用</h3>
              <p className={styles.sectionMeta}>
                默认使用拾迹内置的共享应用。自托管或企业环境可填入自己在 Entra 注册的 Application (client) ID。
              </p>
            </div>
            <div className={styles.formGrid}>
              <label className={styles.field}>
                <span>Application (client) ID</span>
                <input
                  value={clientIdInput}
                  placeholder={usingDefault ? '粘贴自定义 client ID' : `当前 ···${clientTail}，可重新输入覆盖`}
                  onChange={(event) => setClientIdInput(event.target.value)}
                />
              </label>
            </div>
            <div className={styles.actionRow}>
              <button
                className={workspaceStyles.button}
                type="button"
                onClick={() => void handleSaveClientId()}
                disabled={busy !== null || !clientIdInput.trim()}
              >
                {busy === 'save' ? '保存中...' : '保存自定义应用'}
              </button>
              {!usingDefault && (
                <button
                  className={workspaceStyles.ghostButton}
                  type="button"
                  onClick={() => setConfirmRestore(true)}
                  disabled={busy !== null}
                >
                  {busy === 'restore' ? '恢复中...' : '恢复内置应用'}
                </button>
              )}
            </div>
          </div>
        )}
      </section>

      <ConfirmDialog
        isOpen={confirmRestore}
        title="恢复内置应用"
        message="恢复内置应用会清除当前自定义应用 ID 和登录状态，之后需要重新登录。确定继续吗？"
        confirmText="恢复内置应用"
        danger
        onConfirm={() => void handleRestoreDefault()}
        onCancel={() => setConfirmRestore(false)}
      />

      {showMigration && (
        <ScheduleMigrationDialog
          source="settings"
          localEventCount={localAccount?.event_count ?? 0}
          onClose={() => setShowMigration(false)}
          onFinished={() => {
            setShowMigration(false)
            invalidateScheduleStatusCache()
            void reload()
          }}
        />
      )}
    </div>
  )
}

function ModelSettingsPanel({
  form,
  settings,
  saving,
  onChange,
  onSubmit,
}: {
  form: ModelForm
  settings: ModelSettings | null
  saving: boolean
  onChange: (form: ModelForm) => void
  onSubmit: (event: FormEvent) => void
}) {
  const setField = <K extends keyof ModelForm>(key: K, value: ModelForm[K]) => {
    onChange({ ...form, [key]: value })
  }

  return (
    <form className={styles.settingsStack} onSubmit={onSubmit}>
      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>主模型</h2>
            <p className={styles.sectionMeta}>用于人格回复、评论追问、私聊和记忆整理——决定 TA 们的回复质量。</p>
          </div>
          <StatusPill ok={settings?.configured ?? false} label={settings?.configured ? '已配置' : '未完成'} />
        </div>
        <div className={styles.formGrid}>
          <TextField
            label="Base URL"
            value={form.base_url}
            onChange={(value) => setField('base_url', value)}
          />
          <TextField
            label="Model"
            value={form.model}
            onChange={(value) => setField('model', value)}
          />
          <TextField
            label="API Key"
            type="password"
            value={form.api_key}
            placeholder={settings?.api_key_masked ?? '输入新的 API Key'}
            onChange={(value) => setField('api_key', value)}
          />
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>副模型（可选）</h2>
            <p className={styles.sectionMeta}>用于搜索判断、查询改写、待办与目标抽取等轻量任务；留空时由主模型承担。配置一个更快的小模型可以缩短回复等待。</p>
          </div>
          <div className={styles.headerControls}>
            <StatusPill
              ok={settings?.secondary_configured ?? false}
              label={settings?.secondary_configured ? '已配置' : '使用主模型'}
            />
            <label className={styles.toggleRow}>
              <input
                type="checkbox"
                checked={form.reuse_secondary_config}
                onChange={(event) => setField('reuse_secondary_config', event.target.checked)}
              />
              <span>复用主模型配置</span>
            </label>
          </div>
        </div>
        <div className={styles.formGrid}>
          <TextField
            label="Model"
            value={form.secondary_model}
            placeholder="留空则使用主模型"
            onChange={(value) => setField('secondary_model', value)}
          />
          {!form.reuse_secondary_config && (
            <>
              <TextField
                label="Base URL"
                value={form.secondary_base_url}
                placeholder={form.base_url || '留空复用主 Base URL'}
                onChange={(value) => setField('secondary_base_url', value)}
              />
              <label className={styles.toggleRow}>
                <input
                  type="checkbox"
                  checked={form.reuse_secondary_api_key}
                  onChange={(event) => setField('reuse_secondary_api_key', event.target.checked)}
                />
                <span>复用主 Key</span>
              </label>
              {!form.reuse_secondary_api_key && (
                <TextField
                  label="独立 API Key"
                  type="password"
                  value={form.secondary_api_key}
                  placeholder={settings?.secondary_api_key_masked ?? '输入副模型 API Key'}
                  onChange={(value) => setField('secondary_api_key', value)}
                />
              )}
            </>
          )}
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>图片识别</h2>
            <p className={styles.sectionMeta}>用于上传图片的视觉摘要、回复和检索。</p>
          </div>
          <div className={styles.headerControls}>
            <StatusPill ok={settings?.vision.configured ?? false} label={settings?.vision.configured ? '可用' : '未启用'} />
            <label className={styles.toggleRow}>
              <input
                type="checkbox"
                checked={form.vision.enabled}
                onChange={(event) => onChange({
                  ...form,
                  vision: { ...form.vision, enabled: event.target.checked },
                })}
              />
              <span>启用</span>
            </label>
          </div>
        </div>
        <div className={styles.formGrid}>
          <TextField
            label="Vision Model"
            value={form.vision.model}
            placeholder="例如 gpt-4o-mini"
            onChange={(value) => onChange({ ...form, vision: { ...form.vision, model: value } })}
          />
          <TextField
            label="Vision Base URL"
            value={form.vision.base_url}
            placeholder={settings?.vision.effective_base_url ?? form.base_url}
            onChange={(value) => onChange({ ...form, vision: { ...form.vision, base_url: value } })}
          />
          <TextField
            label="Vision API Key"
            type="password"
            value={form.vision.api_key}
            placeholder={settings?.vision.api_key_masked ?? '留空复用主 Key'}
            onChange={(value) => onChange({ ...form, vision: { ...form.vision, api_key: value } })}
          />
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>网页搜索</h2>
            <p className={styles.sectionMeta}>由模型自动判断是否需要搜索；DuckDuckGo 免配置，Tavily 体验更稳定但需要 API Key。</p>
          </div>
          <div className={styles.headerControls}>
            <StatusPill
              ok={settings?.web_search.configured ?? false}
              label={webSearchStatusLabel(settings)}
            />
            <label className={styles.toggleRow}>
              <input
                type="checkbox"
                checked={form.web_search.enabled}
                onChange={(event) => onChange({
                  ...form,
                  web_search: { ...form.web_search, enabled: event.target.checked },
                })}
              />
              <span>启用</span>
            </label>
          </div>
        </div>
        <div className={styles.formGrid}>
          <SelectField
            label="服务"
            value={form.web_search.provider}
            options={[
              { value: 'duckduckgo', label: 'DuckDuckGo（免配置）' },
              { value: 'tavily', label: 'Tavily（需 API Key）' },
            ]}
            onChange={(value) => onChange({
              ...form,
              web_search: { ...form.web_search, provider: value as WebSearchProvider },
            })}
          />
          {form.web_search.provider === 'tavily' && (
            <TextField
              label="Tavily API Key"
              type="password"
              value={form.web_search.tavily_api_key}
              placeholder={settings?.web_search.tavily_api_key_masked ?? '选 Tavily 时必填'}
              onChange={(value) => onChange({
                ...form,
                web_search: { ...form.web_search, tavily_api_key: value },
              })}
            />
          )}
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>Embedding</h2>
            <p className={styles.sectionMeta}>用于语义检索和长期记忆索引。</p>
          </div>
          <label className={styles.toggleRow}>
            <input
              type="checkbox"
              checked={form.reuse_embedding_config}
              onChange={(event) => setField('reuse_embedding_config', event.target.checked)}
            />
            <span>复用主配置</span>
          </label>
        </div>
        <div className={styles.formGrid}>
          <TextField
            label="Embedding Model"
            value={form.embedding_model}
            onChange={(value) => setField('embedding_model', value)}
          />
          {!form.reuse_embedding_config && (
            <>
              <TextField
                label="Embedding Base URL"
                value={form.embedding_base_url}
                onChange={(value) => setField('embedding_base_url', value)}
              />
              <TextField
                label="Embedding API Key"
                type="password"
                value={form.embedding_api_key}
                placeholder={settings?.embedding_api_key_masked ?? '输入新的 Embedding Key'}
                onChange={(value) => setField('embedding_api_key', value)}
              />
            </>
          )}
        </div>
        {settings && (
          <p className={styles.vectorIndexStatus}>
            {vectorIndexStatusLabel(settings.vector_index)}
          </p>
        )}
      </section>

      <div className={styles.saveBar}>
        <span className={styles.saveHint}>保存后应用会自动重新加载配置。</span>
        <button className={workspaceStyles.button} type="submit" disabled={saving}>
          {saving ? '保存中...' : '保存配置'}
        </button>
      </div>
    </form>
  )
}

function SoulSettingsPanel({
  souls,
  savingSoul,
  createSoulMode,
  newSoulName,
  newSoulContent,
  preview,
  previewFeedback,
  editing,
  editFeedback,
  onCreateSoulModeChange,
  onNewSoulNameChange,
  onNewSoulContentChange,
  onCreateSoul,
  onToggleSoul,
  onMoveSoul,
  onPreviewContentChange,
  onPreviewFeedbackChange,
  onRegeneratePreview,
  onRefinePreview,
  onConfirmCreateSoul,
  onDiscardPreview,
  onStartEditSoul,
  onEditContentChange,
  onEditFeedbackChange,
  onRefineEditingSoul,
  onSaveEditingSoul,
  onCancelEditSoul,
}: {
  souls: Soul[]
  savingSoul: string | null
  createSoulMode: CreateSoulMode
  newSoulName: string
  newSoulContent: string
  preview: SoulPreview | null
  previewFeedback: string
  editing: SoulEditing | null
  editFeedback: string
  onCreateSoulModeChange: (value: CreateSoulMode) => void
  onNewSoulNameChange: (value: string) => void
  onNewSoulContentChange: (value: string) => void
  onCreateSoul: (event: FormEvent) => void
  onToggleSoul: (soul: Soul) => void
  onMoveSoul: (index: number, direction: -1 | 1) => void
  onPreviewContentChange: (value: string) => void
  onPreviewFeedbackChange: (value: string) => void
  onRegeneratePreview: () => void
  onRefinePreview: () => void
  onConfirmCreateSoul: () => void
  onDiscardPreview: () => void
  onStartEditSoul: (soul: Soul) => void
  onEditContentChange: (value: string) => void
  onEditFeedbackChange: (value: string) => void
  onRefineEditingSoul: () => void
  onSaveEditingSoul: () => void
  onCancelEditSoul: () => void
}) {
  const isAiMode = createSoulMode === 'ai'

  return (
    <div className={styles.settingsStack}>
      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>人格列表</h2>
            <p className={styles.sectionMeta}>排序决定首页并发回应顺序；禁用后不会出现在导航和回应队列里。</p>
          </div>
          <span className={styles.countBadge}>{souls.filter((soul) => soul.enabled).length}/{souls.length} 启用</span>
        </div>
        <div className={styles.soulList}>
          {souls.map((soul, index) => (
            <div className={styles.soulRow} key={soul.name}>
              <SoulAvatar name={soul.name} className={styles.soulAvatar} />
              <div className={styles.soulBody}>
                <div className={styles.soulTitleRow}>
                  <span className={styles.soulName}>{soul.name}</span>
                  <span className={styles.pathText}>{soul.file_path}</span>
                </div>
                <p className={styles.soulDescription}>{soul.description || '暂无描述'}</p>
              </div>
              <div className={styles.rowActions}>
                <button
                  className={styles.iconButton}
                  type="button"
                  onClick={() => onStartEditSoul(soul)}
                  disabled={savingSoul !== null || editing !== null}
                  title="编辑人格文件"
                  aria-label="编辑人格文件"
                >
                  <PencilIcon width={13} height={13} />
                </button>
                <button
                  className={styles.iconButton}
                  type="button"
                  onClick={() => onMoveSoul(index, -1)}
                  disabled={index === 0 || savingSoul !== null}
                  title="上移"
                  aria-label="上移"
                >
                  <ArrowUpIcon />
                </button>
                <button
                  className={styles.iconButton}
                  type="button"
                  onClick={() => onMoveSoul(index, 1)}
                  disabled={index === souls.length - 1 || savingSoul !== null}
                  title="下移"
                  aria-label="下移"
                >
                  <ArrowDownIcon />
                </button>
                <label className={styles.switch}>
                  <input
                    type="checkbox"
                    checked={soul.enabled}
                    disabled={savingSoul !== null}
                    onChange={() => onToggleSoul(soul)}
                  />
                  <span>{soul.enabled ? '启用' : '停用'}</span>
                </label>
              </div>
            </div>
          ))}
          {souls.length === 0 && <div className={workspaceStyles.empty}>还没有人格，用下面的表单创建第一个。</div>}
        </div>
      </section>

      {editing && (
        <section className={styles.section}>
          <div className={styles.sectionHeader}>
            <div>
              <h2 className={styles.sectionTitle}>编辑人格：{editing.name}</h2>
              <p className={styles.sectionMeta}>直接修改 Markdown，或让 AI 按一句话反馈调整。保存后立即生效。</p>
            </div>
          </div>
          <div className={styles.createSoulForm}>
            <label className={styles.textareaField}>
              <span>Markdown 全文</span>
              <textarea
                value={editing.content}
                onChange={(event) => onEditContentChange(event.target.value)}
                rows={14}
              />
            </label>
            <TextField
              label="AI 反馈微调（可选）"
              value={editFeedback}
              onChange={onEditFeedbackChange}
              placeholder="一句话反馈，例如：语气再温柔一点，回应更简短"
            />
            <div className={styles.formActions}>
              <span className={styles.saveHint}>
                {editing.content !== editing.original ? '有未保存的修改。' : '尚无改动。'}
              </span>
              <div className={styles.actionRow}>
                <button
                  className={workspaceStyles.ghostButton}
                  type="button"
                  onClick={onCancelEditSoul}
                  disabled={savingSoul !== null}
                >
                  取消
                </button>
                <button
                  className={workspaceStyles.ghostButton}
                  type="button"
                  onClick={onRefineEditingSoul}
                  disabled={savingSoul !== null || !editFeedback.trim() || !editing.content.trim()}
                >
                  {savingSoul === 'edit-refine' ? 'AI 调整中...' : 'AI 按反馈调整'}
                </button>
                <button
                  className={workspaceStyles.button}
                  type="button"
                  onClick={onSaveEditingSoul}
                  disabled={savingSoul !== null || !editing.content.trim() || editing.content === editing.original}
                >
                  {savingSoul === 'edit-save' ? '保存中...' : '保存修改'}
                </button>
              </div>
            </div>
          </div>
        </section>
      )}

      {preview ? (
        <section className={styles.section}>
          <div className={styles.sectionHeader}>
            <div>
              <h2 className={styles.sectionTitle}>预览：{preview.name}</h2>
              <p className={styles.sectionMeta}>确认或修改后再创建。也可以重新生成，或让 AI 按一句话反馈调整。</p>
            </div>
          </div>
          <div className={styles.createSoulForm}>
            {preview.searchUsed && preview.sources.length > 0 && (
              <details className={styles.inlineDetails}>
                <summary>已参考 {preview.sources.length} 条网络资料</summary>
                {preview.sources.map((source) => (
                  <p key={source.url}>
                    <a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>
                  </p>
                ))}
              </details>
            )}
            <label className={styles.textareaField}>
              <span>生成的 Markdown（可直接编辑）</span>
              <textarea
                value={preview.content}
                onChange={(event) => onPreviewContentChange(event.target.value)}
                rows={14}
              />
            </label>
            <TextField
              label="AI 反馈微调（可选）"
              value={previewFeedback}
              onChange={onPreviewFeedbackChange}
              placeholder="一句话反馈，例如：语气再毒舌一点，边界更严格"
            />
            <div className={styles.formActions}>
              <span className={styles.saveHint}>满意后点「创建人格」，文件才会真正保存。</span>
              <div className={styles.actionRow}>
                <button
                  className={workspaceStyles.ghostButton}
                  type="button"
                  onClick={onDiscardPreview}
                  disabled={savingSoul !== null}
                >
                  放弃预览
                </button>
                <button
                  className={workspaceStyles.ghostButton}
                  type="button"
                  onClick={onRegeneratePreview}
                  disabled={savingSoul !== null}
                >
                  {savingSoul === 'generate' ? '生成中...' : '重新生成'}
                </button>
                <button
                  className={workspaceStyles.ghostButton}
                  type="button"
                  onClick={onRefinePreview}
                  disabled={savingSoul !== null || !previewFeedback.trim() || !preview.content.trim()}
                >
                  {savingSoul === 'refine' ? 'AI 调整中...' : 'AI 按反馈调整'}
                </button>
                <button
                  className={workspaceStyles.button}
                  type="button"
                  onClick={onConfirmCreateSoul}
                  disabled={savingSoul !== null || !preview.content.trim()}
                >
                  {savingSoul === 'new' ? '创建中...' : '创建人格'}
                </button>
              </div>
            </div>
          </div>
        </section>
      ) : (
        <section className={styles.section}>
          <div className={styles.sectionHeader}>
            <div>
              <h2 className={styles.sectionTitle}>新建人格</h2>
              <p className={styles.sectionMeta}>用 AI 整理成 Markdown，或自己写完整 Markdown。</p>
            </div>
          </div>
          <form className={styles.createSoulForm} onSubmit={onCreateSoul}>
            <div className={styles.modeTabs} role="tablist" aria-label="新建人格方式">
              <button
                className={`${styles.modeTab} ${isAiMode ? styles.modeTabActive : ''}`}
                type="button"
                role="tab"
                aria-selected={isAiMode}
                onClick={() => onCreateSoulModeChange('ai')}
              >
                AI 生成 Markdown
              </button>
              <button
                className={`${styles.modeTab} ${!isAiMode ? styles.modeTabActive : ''}`}
                type="button"
                role="tab"
                aria-selected={!isAiMode}
                onClick={() => onCreateSoulModeChange('markdown')}
              >
                手写 Markdown
              </button>
            </div>
            <TextField label="名称" value={newSoulName} onChange={onNewSoulNameChange} />
            <label className={styles.textareaField}>
              <span>{isAiMode ? '灵感描述' : 'Markdown 全文'}</span>
              <textarea
                value={newSoulContent}
                onChange={(event) => onNewSoulContentChange(event.target.value)}
                placeholder={isAiMode ? AI_SOUL_PLACEHOLDER : newSoulMarkdownTemplate(newSoulName)}
                rows={isAiMode ? 7 : 11}
              />
            </label>
            <div className={styles.formActions}>
              <span className={styles.saveHint}>
                {isAiMode
                  ? '生成后会先进入预览，确认或修改后才会创建。提到公开角色/人物时会自动联网参考。'
                  : '内容会直接保存为人格 Markdown 文件。'}
              </span>
              <button
                className={workspaceStyles.button}
                type="submit"
                disabled={!newSoulName.trim() || !newSoulContent.trim() || savingSoul !== null}
              >
                {savingSoul === 'generate'
                  ? '生成中...'
                  : savingSoul === 'new' && !isAiMode
                    ? '创建中...'
                    : isAiMode
                      ? '生成预览'
                      : '创建人格'}
              </button>
            </div>
          </form>
        </section>
      )}
    </div>
  )
}

function DataSettingsPanel({
  status,
  logStats,
  logAction,
  confirmClearLogs,
  logRevealPath,
  vectorAction,
  vectorError,
  onVectorAction,
  onCaptureContentChange,
  onRequestClearLogs,
  onCancelClearLogs,
  onClearLogs,
  onRevealLogs,
}: {
  status: WorkspaceStatus | null
  logStats: LogStats | null
  logAction: 'toggle' | 'clear' | 'reveal' | null
  confirmClearLogs: boolean
  logRevealPath: string | null
  vectorAction: 'retry' | 'reconcile' | null
  vectorError: string | null
  onVectorAction: (action: 'retry' | 'reconcile') => void
  onCaptureContentChange: (captureContent: boolean) => void
  onRequestClearLogs: () => void
  onCancelClearLogs: () => void
  onClearLogs: () => void
  onRevealLogs: () => void
}) {
  if (!status) {
    return <div className={workspaceStyles.empty}>无法读取本地数据状态</div>
  }
  const vectorStatus = vectorIndexStatus(status.vector_index)
  const vectorActionConfig = vectorIndexActionConfig(status.vector_index, vectorAction)

  return (
    <div className={styles.settingsStack}>
      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>Workspace</h2>
            <p className={styles.sectionMeta}>TraceLog 的本地事实源都在这里。</p>
          </div>
          <StatusPill ok={status.workspace_exists && status.db_exists} label={status.db_exists ? '可用' : '缺失'} />
        </div>
        <div className={styles.pathGrid}>
          <PathRow label="SQLite" value={status.db_path} />
          <PathRow label="人格" value={status.souls_dir} />
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>数据概览</h2>
            <p className={styles.sectionMeta}>当前本地库里的主要对象数量。</p>
          </div>
        </div>
        <div className={styles.statGrid}>
          <Stat label="记录" value={status.counts.posts} />
          <Stat label="回应" value={status.counts.comments} />
          <Stat label="人格" value={status.counts.souls} />
          <Stat label="启用人格" value={status.counts.enabled_souls} />
          <Stat label="任务" value={status.counts.jobs} />
          <Stat label="图片摘要" value={status.counts.vision_cache} />
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>向量索引</h2>
            <p className={styles.sectionMeta}>用于记忆检索。通常无需手动处理，异常时按提示同步即可。</p>
          </div>
          <StatusPill ok={vectorStatus.ok} label={vectorStatus.label} />
        </div>
        <div className={styles.sectionBodyStack}>
          <p className={styles.sectionMeta}>{vectorStatus.description}</p>
          {vectorError && (
            <div className={styles.inlineFailure}>
              <div className={styles.inlineFailureMain}>
                <strong>记忆检索失败</strong>
              </div>
              <details className={styles.inlineDetails}>
                <summary>诊断信息</summary>
                <p>{vectorError}</p>
              </details>
            </div>
          )}
          {vectorActionConfig && (
            <div className={styles.actionRow}>
              <button
                className={workspaceStyles.ghostButton}
                type="button"
                onClick={() => onVectorAction(vectorActionConfig.action)}
                disabled={vectorAction !== null}
              >
                {vectorActionConfig.label}
              </button>
            </div>
          )}
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>存储</h2>
            <p className={styles.sectionMeta}>本地数据库当前占用。</p>
          </div>
        </div>
        <div className={styles.statGrid}>
          <Stat label="数据库大小" value={formatBytes(status.db_size_bytes)} />
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <div>
            <h2 className={styles.sectionTitle}>调试日志</h2>
          </div>
        </div>
        <div className={styles.sectionBodyStack}>
          <div className={styles.logToggleRow}>
            <div>
              <p className={styles.logPrimary}>记录完整对话内容</p>
              <p className={styles.sectionMeta}>默认关闭。开启后会在本机保存你与 AI 的完整对话内容；关闭时仍会记录不含内容的调用统计，失败调用会保留截断片段用于排障。</p>
            </div>
            <label className={styles.switch}>
              <input
                type="checkbox"
                aria-label="记录完整对话内容"
                checked={logStats?.capture_content ?? false}
                disabled={!logStats || logAction !== null}
                onChange={(event) => onCaptureContentChange(event.target.checked)}
              />
            </label>
          </div>
          <p className={styles.logStatus}>
            当前日志：{logStats?.file_count ?? 0} 个文件 · {formatLogSize(logStats?.total_bytes ?? 0)}
          </p>
          <div className={styles.actionRow}>
            <button
              className={workspaceStyles.dangerButton}
              type="button"
              disabled={!logStats || logAction !== null}
              onClick={onRequestClearLogs}
            >
              {logAction === 'clear' ? '清空中...' : '清空日志'}
            </button>
            <button
              className={workspaceStyles.ghostButton}
              type="button"
              disabled={!logStats || logAction !== null}
              onClick={onRevealLogs}
            >
              {logAction === 'reveal' ? '打开中...' : '打开日志文件夹'}
            </button>
          </div>
          {logRevealPath && (
            <p className={styles.logRevealFallback}>无法自动打开，请前往：<code>{logRevealPath}</code></p>
          )}
        </div>
      </section>

      <ConfirmDialog
        isOpen={confirmClearLogs}
        title="清空日志"
        message="清空后所有调试日志将被永久删除，且不可恢复。确定继续吗？"
        confirmText="清空日志"
        danger
        onConfirm={onClearLogs}
        onCancel={onCancelClearLogs}
      />
    </div>
  )
}

function vectorIndexStatus(vectorIndex: WorkspaceStatus['vector_index']): {
  ok: boolean
  label: string
  description: string
} {
  if (!vectorIndex.ready) {
    return {
      ok: false,
      label: '待同步',
      description: '记忆检索索引还没有准备好，可以先同步一次。',
    }
  }
  if (vectorIndex.failed_count > 0) {
    return {
      ok: false,
      label: '需要处理',
      description: '有部分记忆检索数据同步失败，可以重试同步。',
    }
  }
  if (
    vectorIndex.pending_count > 0
    || vectorIndex.missing_count > 0
    || vectorIndex.stale_count > 0
    || vectorIndex.source_revision > vectorIndex.synced_revision
  ) {
    return {
      ok: false,
      label: '同步中',
      description: '记忆检索索引正在追上最新记录。',
    }
  }
  return {
    ok: true,
    label: '正常',
    description: '记忆检索索引已准备好。',
  }
}

function vectorIndexActionConfig(
  vectorIndex: WorkspaceStatus['vector_index'],
  vectorAction: 'retry' | 'reconcile' | null,
): { action: 'retry' | 'reconcile'; label: string } | null {
  if (!vectorIndex.ready || vectorIndex.failed_count > 0) {
    return {
      action: 'retry',
      label: vectorAction === 'retry' ? '同步中...' : '重试同步',
    }
  }
  if (
    vectorIndex.pending_count > 0
    || vectorIndex.missing_count > 0
    || vectorIndex.stale_count > 0
    || vectorIndex.source_revision > vectorIndex.synced_revision
  ) {
    return {
      action: 'reconcile',
      label: vectorAction === 'reconcile' ? '同步中...' : '同步索引',
    }
  }
  return null
}

function TextField({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
}: {
  label: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  type?: 'text' | 'password'
}) {
  return (
    <label className={styles.field}>
      <span>{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  )
}

function SelectField<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: Array<{ value: T; label: string }>
  onChange: (value: T) => void
}) {
  return (
    <label className={styles.field}>
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value as T)}>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

function StatusPill({ ok, label }: { ok: boolean; label: string }) {
  return <span className={`${styles.statusPill} ${ok ? styles.statusOk : styles.statusMuted}`}>{label}</span>
}

function PathRow({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.pathRow}>
      <span>{label}</span>
      <code>{value}</code>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statValue}>{value}</span>
      <span className={styles.statLabel}>{label}</span>
    </div>
  )
}

function formFromModelSettings(settings: ModelSettings): ModelForm {
  return {
    api_key: '',
    base_url: settings.base_url,
    model: settings.model,
    embedding_model: settings.embedding_model,
    embedding_api_key: '',
    embedding_base_url: settings.embedding_base_url ?? '',
    reuse_embedding_config: settings.reuse_embedding_config,
    secondary_model: settings.secondary_model ?? '',
    secondary_api_key: '',
    secondary_base_url: settings.secondary_base_url ?? '',
    reuse_secondary_config: settings.reuse_secondary_config ?? true,
    reuse_secondary_api_key: settings.reuse_secondary_api_key ?? !settings.has_secondary_api_key,
    logging: settings.logging,
    vision: {
      enabled: settings.vision?.enabled ?? false,
      model: settings.vision?.model ?? '',
      api_key: '',
      base_url: settings.vision?.base_url ?? '',
    },
    web_search: {
      enabled: settings.web_search?.enabled ?? false,
      provider: settings.web_search?.provider ?? 'duckduckgo',
      tavily_api_key: '',
      max_results: settings.web_search?.max_results ?? 5,
      timeout_s: settings.web_search?.timeout_s ?? 8,
      cache_ttl_s: settings.web_search?.cache_ttl_s ?? 1800,
    },
  }
}

function toModelUpdate(form: ModelForm): ModelSettingsUpdate {
  return {
    api_key: form.api_key.trim() || undefined,
    base_url: form.base_url.trim(),
    model: form.model.trim(),
    embedding_model: form.embedding_model.trim(),
    embedding_api_key: form.embedding_api_key.trim() || undefined,
    embedding_base_url: form.reuse_embedding_config ? null : form.embedding_base_url.trim() || null,
    reuse_embedding_config: form.reuse_embedding_config,
    secondary_model: form.secondary_model.trim() || null,
    secondary_api_key: form.reuse_secondary_config ? undefined : form.secondary_api_key.trim() || undefined,
    secondary_base_url: form.reuse_secondary_config ? null : form.secondary_base_url.trim() || null,
    reuse_secondary_config: form.reuse_secondary_config,
    reuse_secondary_api_key: form.reuse_secondary_config || form.reuse_secondary_api_key,
    logging: form.logging,
    vision: {
      enabled: form.vision.enabled,
      model: form.vision.model.trim() || null,
      api_key: form.vision.api_key.trim() || undefined,
      base_url: form.vision.base_url.trim() || null,
    },
    web_search: {
      enabled: form.web_search.enabled,
      provider: form.web_search.provider,
      tavily_api_key: form.web_search.tavily_api_key.trim() || undefined,
      max_results: form.web_search.max_results,
      timeout_s: form.web_search.timeout_s,
      cache_ttl_s: form.web_search.cache_ttl_s,
    },
  }
}

function webSearchStatusLabel(settings: ModelSettings | null): string {
  const webSearch = settings?.web_search
  if (!webSearch?.enabled) return '已关闭'
  if (!webSearch.configured) return '不可用'
  if (webSearch.selected_provider === 'tavily') return '可用：Tavily'
  if (webSearch.selected_provider === 'duckduckgo') return '可用：DuckDuckGo'
  return '可用'
}

function vectorIndexStatusLabel(status: ModelSettings['vector_index']): string {
  const main = status.ready
    ? `向量索引已就绪（${status.indexed} 条）`
    : `向量索引重建中 ${status.indexed}/${status.total}，期间记忆检索可能不完整`
  return status.failed > 0
    ? `${main}；有 ${status.failed} 条失败将自动重试`
    : main
}

function newSoulMarkdownTemplate(name: string): string {
  const soulName = name.trim()
  return `---
name: ${soulName}
version: 1
description: 简短描述
created_at: ${todayDate()}
author: TraceLog 用户自定义
tags: []
---

你是 TraceLog 中名为「${soulName}」的 AI 好友。

## 语气特征
- ...

## 怎么回应
- ...

## 边界
- 不做医疗、法律、金融等专业结论
- 用户明显痛苦或有安全风险时，优先建议寻求现实支持
`
}

function isDefaultSoulTemplate(content: string, name: string): boolean {
  return content === newSoulMarkdownTemplate(name)
}

function todayDate(): string {
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatLogSize(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

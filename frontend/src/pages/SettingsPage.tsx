import { FormEvent, useEffect, useMemo, useState } from 'react'
import {
  type ModelSettings,
  type ModelSettingsUpdate,
  type Soul,
  type WorkspaceStatus,
  createSoul,
  generateSoul,
  getModelSettings,
  getSoulContent,
  getWorkspaceStatus,
  listSouls,
  reconcileVectorIndex,
  reorderSouls,
  retryVectorIndex,
  saveModelSettings,
  updateSoul,
} from '@/api/client'
import { Notice } from '@/components/Notice'
import { SoulAvatar } from '@/components/SoulAvatar'
import workspaceStyles from './WorkspacePages.module.css'
import styles from './SettingsPage.module.css'

type SettingsTab = 'model' | 'souls' | 'data' | 'about'
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
  logging: {
    enabled: true,
    level: 'INFO',
    history_retention: 100,
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
  data: '本地 workspace 状态、数据概览与记忆检索索引',
  about: '关于拾迹这个项目',
}

export function SettingsPage({ firstRun = false, onModelSettingsChanged, onSoulsChanged }: SettingsPageProps) {
  const [activeTab, setActiveTab] = useState<SettingsTab>('model')
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null)
  const [modelForm, setModelForm] = useState<ModelForm>(DEFAULT_MODEL_FORM)
  const [souls, setSouls] = useState<Soul[]>([])
  const [workspaceStatus, setWorkspaceStatus] = useState<WorkspaceStatus | null>(null)
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
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const tabs = useMemo(
    () => [
      { id: 'model' as const, label: '基本' },
      { id: 'souls' as const, label: '人格' },
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
      const [model, soulList, workspace] = await Promise.all([
        getModelSettings(),
        listSouls(false),
        getWorkspaceStatus(),
      ])
      setModelSettings(model)
      setModelForm(formFromModelSettings(model))
      setSouls(soulList)
      setWorkspaceStatus(workspace)
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
              {activeTab === 'data' && (
                <DataSettingsPanel
                  status={workspaceStatus}
                  vectorAction={vectorAction}
                  vectorError={vectorError}
                  onVectorAction={handleVectorAction}
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

function AboutSettingsPanel() {
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
        <span>版本 v2.0</span>
        <a href="https://github.com/sld272/TraceLog" target="_blank" rel="noreferrer">
          GitHub 仓库
        </a>
      </div>
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
            <p className={styles.sectionMeta}>用于公开回应、追问、私聊和整理。</p>
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
                >
                  ✎
                </button>
                <button
                  className={styles.iconButton}
                  type="button"
                  onClick={() => onMoveSoul(index, -1)}
                  disabled={index === 0 || savingSoul !== null}
                  title="上移"
                >
                  ↑
                </button>
                <button
                  className={styles.iconButton}
                  type="button"
                  onClick={() => onMoveSoul(index, 1)}
                  disabled={index === souls.length - 1 || savingSoul !== null}
                  title="下移"
                >
                  ↓
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
  vectorAction,
  vectorError,
  onVectorAction,
}: {
  status: WorkspaceStatus | null
  vectorAction: 'retry' | 'reconcile' | null
  vectorError: string | null
  onVectorAction: (action: 'retry' | 'reconcile') => void
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
          <Stat label="待办" value={status.counts.todos} />
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

import { FormEvent, useEffect, useMemo, useState } from 'react'
import {
  type ModelSettings,
  type ModelSettingsUpdate,
  type Soul,
  type WorkspaceStatus,
  createSoul,
  generateSoul,
  getModelSettings,
  getWorkspaceStatus,
  listSouls,
  reconcileVectorIndex,
  reorderSouls,
  retryVectorIndex,
  saveModelSettings,
  updateSoul,
} from '@/api/client'
import { MemorySettingsPanel } from './MemorySettingsPanel'
import workspaceStyles from './WorkspacePages.module.css'
import styles from './SettingsPage.module.css'

type SettingsTab = 'model' | 'souls' | 'memory' | 'data'
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
    history_retention: 5,
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

export function SettingsPage({ firstRun = false, onModelSettingsChanged, onSoulsChanged }: SettingsPageProps) {
  const [activeTab, setActiveTab] = useState<SettingsTab>('model')
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null)
  const [modelForm, setModelForm] = useState<ModelForm>(DEFAULT_MODEL_FORM)
  const [souls, setSouls] = useState<Soul[]>([])
  const [workspaceStatus, setWorkspaceStatus] = useState<WorkspaceStatus | null>(null)
  const [createSoulMode, setCreateSoulMode] = useState<CreateSoulMode>('ai')
  const [aiSoulDraft, setAiSoulDraft] = useState<AiSoulDraft>({ name: '', inspiration: '' })
  const [markdownSoulDraft, setMarkdownSoulDraft] = useState<MarkdownSoulDraft>({ name: '', content: '' })
  const [loading, setLoading] = useState(true)
  const [savingModel, setSavingModel] = useState(false)
  const [savingSoul, setSavingSoul] = useState<string | null>(null)
  const [vectorAction, setVectorAction] = useState<'retry' | 'reconcile' | null>(null)
  const [vectorError, setVectorError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const tabs = useMemo(
    () => [
      { id: 'model' as const, label: '模型' },
      { id: 'souls' as const, label: '人格' },
      { id: 'memory' as const, label: '记忆' },
      { id: 'data' as const, label: '数据' },
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
    setSavingSoul('new')
    setNotice(null)
    setError(null)
    try {
      const soul = createSoulMode === 'ai'
        ? (await generateSoul(name, content)).soul
        : content
      const created = await createSoul(name, null, true, soul)
      setSouls((items) => [...items, created].sort((a, b) => a.sort_order - b.sort_order))
      if (createSoulMode === 'ai') {
        setAiSoulDraft({ name: '', inspiration: '' })
      } else {
        setMarkdownSoulDraft({ name: '', content: '' })
      }
      setNotice(`已创建 ${created.name}`)
      onSoulsChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建人格失败')
    } finally {
      setSavingSoul(null)
    }
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
          <p className={workspaceStyles.subtitle}>模型、人格、记忆与本地数据</p>
        </div>
        <button className={workspaceStyles.ghostButton} onClick={refreshSettings} disabled={loading}>
          刷新
        </button>
      </header>

      {error && <div className={workspaceStyles.notice}>{error}</div>}
      {firstRun && (
        <div className={workspaceStyles.notice}>首次使用 TraceLog，请先配置主模型和 Embedding。保存后应用会自动重新加载配置。</div>
      )}
      {notice && <div className={styles.successNotice}>{notice}</div>}

      <div className={styles.tabs} role="tablist" aria-label="设置分类">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={`${styles.tab} ${activeTab === tab.id ? styles.tabActive : ''}`}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            role="tab"
            aria-selected={activeTab === tab.id}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className={workspaceStyles.empty}>加载设置中...</div>
      ) : (
        <div className={styles.content}>
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
              onCreateSoulModeChange={handleCreateSoulModeChange}
              onNewSoulNameChange={handleNewSoulNameChange}
              onNewSoulContentChange={handleNewSoulContentChange}
              onCreateSoul={handleCreateSoul}
              onToggleSoul={handleToggleSoul}
              onMoveSoul={handleMoveSoul}
            />
          )}
          {activeTab === 'memory' && (
            <MemorySettingsPanel souls={souls} workspaceStatus={workspaceStatus} />
          )}
          {activeTab === 'data' && (
            <DataSettingsPanel
              status={workspaceStatus}
              vectorAction={vectorAction}
              vectorError={vectorError}
              onVectorAction={handleVectorAction}
            />
          )}
        </div>
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
            <p className={styles.sectionMeta}>用于公开回应、私聊、评论和反思。</p>
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
  onCreateSoulModeChange,
  onNewSoulNameChange,
  onNewSoulContentChange,
  onCreateSoul,
  onToggleSoul,
  onMoveSoul,
}: {
  souls: Soul[]
  savingSoul: string | null
  createSoulMode: CreateSoulMode
  newSoulName: string
  newSoulContent: string
  onCreateSoulModeChange: (value: CreateSoulMode) => void
  onNewSoulNameChange: (value: string) => void
  onNewSoulContentChange: (value: string) => void
  onCreateSoul: (event: FormEvent) => void
  onToggleSoul: (soul: Soul) => void
  onMoveSoul: (index: number, direction: -1 | 1) => void
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
              <div className={styles.soulAvatar}>{soul.name.charAt(0).toUpperCase()}</div>
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
          {souls.length === 0 && <div className={workspaceStyles.empty}>暂无人格</div>}
        </div>
      </section>

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
              {isAiMode ? '系统会把你的描述整理成完整人格 Markdown 文件。' : '内容会直接保存为人格 Markdown 文件。'}
            </span>
            <button
              className={workspaceStyles.button}
              type="submit"
              disabled={!newSoulName.trim() || !newSoulContent.trim() || savingSoul !== null}
            >
              {savingSoul === 'new' ? (isAiMode ? '生成中...' : '创建中...') : (isAiMode ? '生成并创建' : '创建人格')}
            </button>
          </div>
        </form>
      </section>
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
          <PathRow label="用户画像" value={status.user_memory_path} />
          <PathRow label="人格" value={status.souls_dir} />
          <PathRow label="人格记忆" value={status.soul_memories_dir} />
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
          <Stat label="动态" value={status.counts.posts} />
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
author: TraceLog 用户
tags: []
---

你是......

## 语气特征
- ...

## 边界
- ...
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

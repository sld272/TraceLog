/* API client for TraceLog backend */

const BASE = '/api'

export class ApiError extends Error {
  readonly status: number
  readonly code: string | null

  constructor(message: string, status: number, code: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = body.detail
    const code = detail && typeof detail === 'object' && typeof detail.code === 'string'
      ? detail.code
      : null
    const message = typeof detail === 'string' ? detail : code ?? `HTTP ${res.status}`
    throw new ApiError(message, res.status, code)
  }
  return res.json()
}

/* Types */
export interface Post {
  post_id: string
  ts: string
  content: string
  importance: number
  comment_count: number
  latest_event_type: string | null
  pipeline_status?: PipelineStatus
  attachments: Attachment[]
}

export type SearchMatchKind = 'keyword' | 'semantic' | 'both'
export type SearchMode = 'keyword' | 'hybrid'

export interface SearchResultItem extends Post {
  match: SearchMatchKind
}

export interface SearchPostsResponse {
  items: SearchResultItem[]
  semantic_available: boolean
  mode: SearchMode
}

export interface Comment {
  id: number
  post_id: string
  soul_name: string
  role: string
  content: string
  seq: number
  metadata: string | null
  created_at: number
  edited_at?: number | null
  rerun_at?: number | null
  attachments: Attachment[]
}

export interface PostDetail {
  post: {
    post_id: string
    ts: string
    content: string
    importance: number
    created_at: number
    updated_at: number
    attachments: Attachment[]
    latest_event_type?: string | null
    pipeline_status?: PipelineStatus
  }
  comments: Comment[]
  jobs: Job[]
  events: PostEvent[]
}

export interface Attachment {
  id: string
  file_path: string
  mime_type: string
  file_size: number
  width: number
  height: number
  sha256: string
  original_filename: string | null
  linked_at: number | null
  created_at: number
  url: string
}

export interface Soul {
  name: string
  file_path: string
  enabled: boolean
  sort_order: number
  description: string | null
  created_at: number
  updated_at: number
}

export type GoalHorizon = 'short' | 'long'
export type GoalStatus = 'active' | 'done' | 'abandoned' | 'paused'

export interface Goal {
  id: string
  title: string
  detail: string | null
  horizon: GoalHorizon
  status: GoalStatus
  source: 'user' | 'suggested_accepted'
  focus: boolean
  last_progress_at: number | null
  created_at: number
  updated_at: number
}

interface SuggestionBase {
  id: string
  evidence_ref: string | null
  confidence: number
  status: 'pending' | 'accepted' | 'dismissed'
  normalized_key: string | null
  created_at: number
  decided_at: number | null
}

export interface GoalSuggestion extends SuggestionBase {
  kind: 'goal'
  payload: {
    title: string
    detail: string | null
    horizon: GoalHorizon
    focus: boolean
  }
}

export interface ScheduleSuggestion extends SuggestionBase {
  kind: 'schedule'
  payload: {
    subject: string
    date: string
    start_time: string | null
    end_time: string | null
    all_day: boolean
    goal_id: string | null
  }
}

export type Suggestion = GoalSuggestion | ScheduleSuggestion

export interface ChatThread {
  id: number
  soul_name: string
  title: string | null
  created_at: number
  updated_at: number
  last_message_at: number | null
}

export interface ChatMessage {
  id: number
  thread_id: number
  role: string
  content: string
  created_at: number
  edited_at?: number | null
  rerun_at?: number | null
  metadata?: string | null
  attachments: Attachment[]
}

export interface CommentConversation {
  post_id: string
  soul_name: string
  root_comment_id: number | null
  created_at: number | null
  updated_at: number | null
  last_message_at: number | null
}

export interface CommentMessage {
  id: number
  post_id: string
  soul_name: string
  role: string
  content: string
  seq: number
  created_at: number
  edited_at?: number | null
  rerun_at?: number | null
  metadata?: string | null
  attachments: Attachment[]
}

export type PostEventType =
  | 'post_created'
  | 'embedding_started'
  | 'embedding_succeeded'
  | 'embedding_failed'
  | 'reply_started'
  | 'reply_succeeded'
  | 'reply_failed'
  | 'pipeline_done'

export interface PostEvent {
  id: number
  post_id: string
  job_id: number | null
  event_type: PostEventType
  payload: unknown
  created_at: number
}

export interface ChatReplyResult {
  thread_id: number
  soul_name: string
  ok: boolean
  reply: string
  user_message_id: number
  assistant_message_id: number | null
  error: string | null
  suggestions: Suggestion[]
}

export interface CommentReplyResult {
  post_id: string
  soul_name: string
  ok: boolean
  reply: string
  user_message_id: number
  assistant_message_id: number | null
  error: string | null
  suggestions: Suggestion[]
}

export type EvidenceChannel = 'chat' | 'comment' | 'public_post'

export interface EvidenceItem {
  doc_id: string
  type: 'post' | 'post_vision' | 'comment' | 'chat' | string
  source_id: string
  post_id: string | null
  score: number | null
  distance: number | null
  sources: string[]
  reasons: string[]
  snippet: string
}

export interface EvidenceFeedbackResult {
  id: number | null
  channel: EvidenceChannel
  message_id: number
  doc_id: string
  verdict: 'irrelevant'
  created_at: number
  created: boolean
}

export interface JobQueued {
  job_id: number | null
  status: string
}

export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface Job {
  id: number
  type: string
  status: JobStatus
  payload_json: string | null
  payload: unknown
  attempts: number
  max_attempts: number
  error: string | null
  created_at: number
  updated_at: number
  started_at: number | null
  finished_at: number | null
}

export interface PipelineJobSummary {
  id: number
  type: string
  status: JobStatus
  attempts: number
  max_attempts: number
  error: string | null
  retryable: boolean
}

export type PipelineState = 'idle' | 'running' | 'retrying' | 'failed' | 'done'

export interface PipelineStatus {
  state: PipelineState
  pending_count: number
  running_count: number
  retrying_count: number
  failed_jobs: PipelineJobSummary[]
}

export interface ModelSettings {
  configured: boolean
  has_api_key: boolean
  api_key_masked: string | null
  base_url: string
  model: string
  embedding_model: string
  has_embedding_api_key: boolean
  embedding_api_key_masked: string | null
  embedding_base_url: string | null
  reuse_embedding_config: boolean
  secondary_model: string | null
  secondary_configured: boolean
  has_secondary_api_key: boolean
  secondary_api_key_masked: string | null
  secondary_base_url: string | null
  reuse_secondary_config: boolean
  reuse_secondary_api_key: boolean
  logging: {
    enabled: boolean
    level: string
    capture_content: boolean
    rotate_max_bytes: number
    history_max_bytes: number
    history_max_days: number
  }
  vision: {
    enabled: boolean
    configured: boolean
    model: string | null
    has_api_key: boolean
    api_key_masked: string | null
    base_url: string | null
    effective_base_url: string | null
    prompt_version: string
    timeout_s: number
  }
  web_search: {
    enabled: boolean
    configured: boolean
    provider: 'tavily' | 'duckduckgo'
    selected_provider: string | null
    tavily_configured: boolean
    duckduckgo_available: boolean
    has_tavily_api_key: boolean
    tavily_api_key_masked: string | null
    max_results: number
    timeout_s: number
    cache_ttl_s: number
  }
  config_path: string
  config_reloaded?: boolean
  restart_required?: boolean
  runtime_reloaded?: boolean
  reload_error?: string
}

export interface ModelSettingsUpdate {
  api_key?: string
  base_url: string
  model: string
  embedding_model: string
  embedding_api_key?: string
  embedding_base_url?: string | null
  reuse_embedding_config: boolean
  secondary_model?: string | null
  secondary_api_key?: string
  secondary_base_url?: string | null
  reuse_secondary_config: boolean
  reuse_secondary_api_key: boolean
  logging: ModelSettings['logging']
  vision: {
    enabled: boolean
    model?: string | null
    api_key?: string
    base_url?: string | null
  }
  web_search: {
    enabled: boolean
    provider: 'tavily' | 'duckduckgo'
    tavily_api_key?: string
    max_results: number
    timeout_s: number
    cache_ttl_s: number
  }
}

export interface WorkspaceStatus {
  workspace_path: string
  workspace_exists: boolean
  db_path: string
  db_exists: boolean
  db_size_bytes: number
  souls_dir: string
  counts: {
    posts: number
    comments: number
    souls: number
    enabled_souls: number
    jobs: number
    vision_cache: number
    memory_units: number
    memory_views: number
  }
  web_search: ModelSettings['web_search']
  vector_index: {
    collection_name: string | null
    embedding_config_hash: string | null
    source_revision: number
    synced_revision: number
    ready: boolean
    pending_count: number
    failed_count: number
    missing_count: number
    stale_count: number
  }
  logs: {
    current_log_path: string
    current_log_exists: boolean
    current_log_size_bytes: number
    history_dir: string
    history_count: number
  }
}

export interface VectorIndexActionResult {
  processed: number
  vector_index: WorkspaceStatus['vector_index']
}

export interface LogStats {
  enabled: boolean
  capture_content: boolean
  file_count: number
  total_bytes: number
  path: string
}

export interface LogRevealResult {
  ok: boolean
  path: string
}

/* Posts */
const DEFAULT_LIST_LIMIT = 20

export function listPosts(
  limit = DEFAULT_LIST_LIMIT,
  offset = 0,
  cursor?: { beforeTs: string; beforeId: string },
) {
  const search = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (cursor) {
    search.set('before_ts', cursor.beforeTs)
    search.set('before_id', cursor.beforeId)
  }
  return request<Post[]>(`/posts?${search.toString()}`)
}

export function searchPosts(q: string, limit = DEFAULT_LIST_LIMIT, mode: SearchMode = 'keyword') {
  return request<SearchPostsResponse>(
    `/posts/search?q=${encodeURIComponent(q)}&limit=${limit}&mode=${mode}`,
  )
}

export function getPost(postId: string) {
  return request<PostDetail>(`/posts/${postId}`)
}

export function createPost(content: string, attachmentIds: string[] = []) {
  return request<{ post_id: string; status: string; job_ids: number[] }>(
    '/posts',
    { method: 'POST', body: JSON.stringify({ content, attachment_ids: attachmentIds }) },
  )
}

export function deletePost(postId: string) {
  return request<{ ok: boolean; post_id: string; deleted_comments: number; cancelled_jobs: number }>(
    `/posts/${encodeURIComponent(postId)}`,
    { method: 'DELETE' },
  )
}

export async function uploadAttachment(file: File): Promise<Attachment> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/attachments/upload`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

export function attachmentUrl(attachment: Attachment): string {
  return `${BASE}${attachment.url}`
}

/** A reply's cited memory: a belief UNIT, or raw FRESH evidence not yet
 *  reconciled into a unit (the [尚未稳定沉淀的原始证据] layer). */
export interface MemoryCitation {
  kind: 'unit' | 'fresh'
  content: string
  // unit-only
  unit_id?: string
  type?: string
  confidence?: number
  // fresh-only
  channel?: string
}

/** The memory a reply actually used — belief units + raw freshness evidence,
 *  stored under metadata.memory_citations. This is the real "引用记忆". */
export function parseMessageMemoryCitations(metadata: string | null | undefined): MemoryCitation[] {
  if (!metadata) return []
  try {
    const parsed = JSON.parse(metadata) as { memory_citations?: { items?: unknown } }
    const items = parsed.memory_citations?.items
    if (!Array.isArray(items)) return []
    return items
      .map((raw): MemoryCitation | null => {
        if (!raw || typeof raw !== 'object') return null
        const o = raw as Record<string, unknown>
        const content = typeof o.content === 'string' ? o.content : ''
        if (!content) return null
        if (o.kind === 'fresh') {
          return { kind: 'fresh', content, channel: typeof o.channel === 'string' ? o.channel : '' }
        }
        const unitId = typeof o.unit_id === 'string' ? o.unit_id : ''
        if (!unitId) return null
        return {
          kind: 'unit',
          content,
          unit_id: unitId,
          type: typeof o.type === 'string' ? o.type : '',
          confidence: typeof o.confidence === 'number' ? o.confidence : 0,
        }
      })
      .filter((item): item is MemoryCitation => item !== null)
  } catch {
    return []
  }
}

export function parseMessageEvidence(metadata: string | null | undefined): EvidenceItem[] {
  if (!metadata) return []
  try {
    const parsed = JSON.parse(metadata) as { evidence?: { items?: unknown } }
    const items = parsed.evidence?.items
    if (!Array.isArray(items)) return []
    return items
      .map(normalizeEvidenceItem)
      .filter((item): item is EvidenceItem => item !== null)
  } catch {
    return []
  }
}

export function parseMessageSuggestions(metadata: string | null | undefined): Suggestion[] {
  if (!metadata) return []
  try {
    const parsed = JSON.parse(metadata) as { suggestions?: unknown }
    if (!Array.isArray(parsed.suggestions)) return []
    return parsed.suggestions.filter(isSuggestion)
  } catch {
    return []
  }
}

function isSuggestion(value: unknown): value is Suggestion {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return typeof item.id === 'string'
    && (item.kind === 'goal' || item.kind === 'schedule')
    && item.status === 'pending'
    && typeof item.payload === 'object'
    && item.payload !== null
}

export function submitEvidenceFeedback(channel: EvidenceChannel, messageId: number, docId: string) {
  return request<EvidenceFeedbackResult>('/feedback/evidence', {
    method: 'POST',
    body: JSON.stringify({
      channel,
      message_id: messageId,
      doc_id: docId,
      verdict: 'irrelevant',
    }),
  })
}

function normalizeEvidenceItem(item: unknown): EvidenceItem | null {
  if (!item || typeof item !== 'object') return null
  const raw = item as Record<string, unknown>
  const docId = stringValue(raw.doc_id)
  if (!docId) return null
  return {
    doc_id: docId,
    type: stringValue(raw.type) || 'post',
    source_id: stringValue(raw.source_id),
    post_id: nullableString(raw.post_id),
    score: nullableNumber(raw.score),
    distance: nullableNumber(raw.distance),
    sources: stringArray(raw.sources),
    reasons: stringArray(raw.reasons),
    snippet: stringValue(raw.snippet) || '(原始内容已删除)',
  }
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : value === null || value === undefined ? '' : String(value)
}

function nullableString(value: unknown): string | null {
  const text = stringValue(value).trim()
  return text ? text : null
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(stringValue).filter(Boolean) : []
}

/* SSE stream for post events */
const POST_EVENT_TYPES: PostEventType[] = [
  'post_created',
  'embedding_started',
  'embedding_succeeded',
  'embedding_failed',
  'reply_started',
  'reply_succeeded',
  'reply_failed',
  'pipeline_done',
]

export function streamPostEvents(
  postId: string,
  onEvent: (event: PostEvent) => void,
  onDone?: () => void,
  options: { afterEventId?: number } = {},
): () => void {
  const query = options.afterEventId !== undefined ? `?after_id=${options.afterEventId}` : ''
  const es = new EventSource(`${BASE}/posts/${postId}/events${query}`)

  POST_EVENT_TYPES.forEach((type) => {
    es.addEventListener(type, (e) => {
      const data = parseSseJson<PostEvent>(e.data, 'post event')
      if (!data) return
      onEvent(data)
      if (data.event_type === 'pipeline_done') {
        onDone?.()
        es.close()
      }
    })
  })

  return () => es.close()
}

export function streamChatMessages(
  threadId: number,
  onMessage: (message: ChatMessage) => void,
  options: { afterId?: number } = {},
): () => void {
  const query = options.afterId !== undefined ? `?after_id=${options.afterId}` : ''
  const es = new EventSource(`${BASE}/chat/threads/${threadId}/events${query}`)

  es.addEventListener('chat_message', (e) => {
    const data = parseSseJson<ChatMessage>(e.data, 'chat message')
    if (!data) return
    onMessage(data)
  })

  return () => es.close()
}

function parseSseJson<T>(data: string, label: string): T | null {
  try {
    return JSON.parse(data) as T
  } catch (err) {
    console.warn(`Invalid ${label} SSE payload`, err)
    return null
  }
}

/* Souls */
export function listSouls(enabledOnly = false) {
  return request<Soul[]>(`/souls?enabled_only=${enabledOnly}`)
}

export function createSoul(name: string, description: string | null, enabled = true, soul?: string) {
  return request<Soul>('/souls', {
    method: 'POST',
    body: JSON.stringify({ name, description, enabled, soul }),
  })
}

export interface GenerateSoulResult {
  soul: string
  search_used: boolean
  sources: { title: string; url: string }[]
}

export function generateSoul(
  name: string,
  inspiration: string,
  revision?: { currentSoul: string; feedback: string },
) {
  return request<GenerateSoulResult>('/souls/generate-soul', {
    method: 'POST',
    body: JSON.stringify({
      name,
      inspiration,
      current_soul: revision?.currentSoul,
      feedback: revision?.feedback,
    }),
  })
}

export function getSoulContent(name: string) {
  return request<{ name: string; soul: string }>(`/souls/${encodeURIComponent(name)}/content`)
}

export function updateSoul(name: string, changes: { enabled?: boolean; description?: string; soul?: string }) {
  return request<Soul>(`/souls/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  })
}

export function reorderSouls(order: string[]) {
  const name = order[0] ?? '_'
  return request<{ souls: Soul[] }>(`/souls/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    body: JSON.stringify({ order }),
  })
}

/* Goals */
export function listGoals(filters: { status?: GoalStatus; horizon?: GoalHorizon } = {}) {
  const search = new URLSearchParams()
  if (filters.status) search.set('status', filters.status)
  if (filters.horizon) search.set('horizon', filters.horizon)
  const suffix = search.toString() ? `?${search.toString()}` : ''
  return request<Goal[]>(`/goals${suffix}`)
}

export function createGoal(
  changes: Pick<Goal, 'title' | 'horizon'> & Partial<Pick<Goal, 'detail' | 'focus'>>,
) {
  return request<Goal>('/goals', {
    method: 'POST',
    body: JSON.stringify(changes),
  })
}

export function updateGoal(goalId: string, changes: Partial<Goal>) {
  return request<Goal>(`/goals/${goalId}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  })
}

export function markGoalProgress(goalId: string) {
  return request<Goal>(`/goals/${goalId}/progress`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function deleteGoal(goalId: string) {
  return request<{ ok: boolean }>(`/goals/${goalId}`, {
    method: 'DELETE',
  })
}

/* Suggestions */
export function listPendingSuggestions(kind?: Suggestion['kind']): Promise<Suggestion[]> {
  const suffix = kind ? `?kind=${kind}` : ''
  return request<Suggestion[]>(`/suggestions${suffix}`)
}

/** Extract the post id from a suggestion's evidence_ref (e.g. "post:20260622-003"). */
export function postIdFromEvidenceRef(ref: string | null | undefined): string | null {
  if (!ref) return null
  const match = /^post:(.+)$/.exec(ref)
  return match?.[1] ?? null
}

export function acceptSuggestion(
  suggestionId: string,
  opts?: { fallbackLocal?: boolean; overrides?: Record<string, unknown> },
) {
  return request<{ suggestion: Suggestion; created: Goal | ScheduleEvent | null }>(
    `/suggestions/${suggestionId}/accept`,
    {
      method: 'POST',
      body: JSON.stringify({
        fallback_local: opts?.fallbackLocal ?? false,
        ...(opts?.overrides ? { overrides: opts.overrides } : {}),
      }),
    },
  )
}

export function dismissSuggestion(suggestionId: string) {
  return request<Suggestion>(`/suggestions/${suggestionId}/dismiss`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

/* Chat */
const DEFAULT_MESSAGE_LIMIT = 30

export function listChatThreads(soulName: string) {
  return request<ChatThread[]>(`/chat/${soulName}/threads`)
}

export function getChatThread(threadId: number, limit = DEFAULT_MESSAGE_LIMIT, beforeMessageId?: number) {
  const beforeQuery = beforeMessageId !== undefined ? `&before_message_id=${beforeMessageId}` : ''
  return request<{ thread: ChatThread; messages: ChatMessage[] }>(
    `/chat/threads/${threadId}?limit=${limit}${beforeQuery}`,
  )
}

export function sendChatMessage(
  soulName: string,
  content: string,
  attachmentIds: string[] = [],
  requestId = crypto.randomUUID(),
) {
  return request<{ thread: ChatThread; result: ChatReplyResult; messages: ChatMessage[] }>(
    `/chat/${soulName}/messages`,
    {
      method: 'POST',
      body: JSON.stringify({ content, attachment_ids: attachmentIds, request_id: requestId }),
    },
  )
}

/**
 * Stream a private-chat reply. Calls onDelta with each incremental text chunk,
 * resolving with the final ChatReplyResult (the SSE `done` frame). Rejects on an
 * `error` frame, a non-OK response, or a missing `done` — the caller can then
 * fall back to the non-streaming sendChatMessage.
 */
export async function sendChatMessageStream(
  soulName: string,
  content: string,
  attachmentIds: string[],
  requestId: string,
  onDelta: (text: string) => void,
): Promise<ChatReplyResult> {
  const res = await fetch(`${BASE}/chat/${soulName}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, attachment_ids: attachmentIds, request_id: requestId }),
  })
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}))
    throw new ApiError(body.detail || `HTTP ${res.status}`, res.status)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result: ChatReplyResult | null = null
  let errorMessage: string | null = null

  const handleFrame = (frame: string) => {
    const { event, data } = parseSseFrame(frame)
    if (!data) return
    if (event === 'delta') {
      const parsed = parseSseJson<{ text?: string }>(data, 'chat delta')
      if (parsed?.text) onDelta(parsed.text)
    } else if (event === 'done') {
      result = parseSseJson<ChatReplyResult>(data, 'chat done')
    } else if (event === 'error') {
      const parsed = parseSseJson<{ message?: string }>(data, 'chat error')
      errorMessage = parsed?.message || '流式回复失败'
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      handleFrame(buffer.slice(0, boundary))
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')
    }
  }
  if (buffer.trim()) handleFrame(buffer)

  if (errorMessage !== null) throw new Error(errorMessage)
  if (!result) throw new Error('流式回复未完成')
  return result
}

/** Parse one SSE frame (its `event:` and joined `data:` lines). */
function parseSseFrame(frame: string): { event: string; data: string } {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  return { event, data: dataLines.join('\n') }
}

export function updateChatMessage(messageId: number, content: string, attachmentIds: string[] = []) {
  return request<{ thread: ChatThread; message: ChatMessage; result: ChatReplyResult; messages: ChatMessage[] }>(
    `/chat/messages/${messageId}`,
    { method: 'PATCH', body: JSON.stringify({ content, attachment_ids: attachmentIds }) },
  )
}

export function rerunChatMessage(messageId: number) {
  return request<{ thread: ChatThread; message: ChatMessage; messages: ChatMessage[] }>(
    `/chat/messages/${messageId}/rerun`,
    { method: 'POST', body: JSON.stringify({}) },
  )
}

/* Jobs */
export function listJobs(params: { status?: JobStatus; job_type?: string; limit?: number; offset?: number } = {}) {
  const search = new URLSearchParams()
  if (params.status) search.set('status', params.status)
  if (params.job_type) search.set('job_type', params.job_type)
  if (params.limit !== undefined) search.set('limit', String(params.limit))
  if (params.offset !== undefined) search.set('offset', String(params.offset))
  const suffix = search.toString() ? `?${search.toString()}` : ''
  return request<Job[]>(`/jobs${suffix}`)
}

export function getJob(jobId: number) {
  return request<Job>(`/jobs/${jobId}`)
}

export function retryJob(jobId: number) {
  return request<JobQueued>(`/jobs/${jobId}/retry`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function cancelJob(jobId: number) {
  return request<JobQueued>(`/jobs/${jobId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

/* Comment conversations */
export function listCommentConversations(postId: string) {
  return request<CommentConversation[]>(
    `/comments/posts/${encodeURIComponent(postId)}/conversations`,
  )
}

export function getCommentConversation(postId: string, soulName: string, limit = DEFAULT_MESSAGE_LIMIT) {
  return request<{ conversation: CommentConversation; messages: CommentMessage[] }>(
    `/comments/posts/${encodeURIComponent(postId)}/souls/${encodeURIComponent(soulName)}?limit=${limit}`,
  )
}

export function sendCommentMessage(postId: string, soulName: string, content: string, attachmentIds: string[] = []) {
  return request<{
    conversation: CommentConversation
    result: CommentReplyResult
    messages: CommentMessage[]
  }>(
    `/comments/posts/${encodeURIComponent(postId)}/souls/${encodeURIComponent(soulName)}/messages`,
    { method: 'POST', body: JSON.stringify({ content, attachment_ids: attachmentIds }) },
  )
}

export function deleteCommentMessage(commentId: number) {
  return request<{ ok: boolean; post_id: string; soul_name: string; deleted_message_ids: number[] }>(
    `/comments/messages/${commentId}`,
    { method: 'DELETE' },
  )
}

export function rerunCommentMessage(commentId: number) {
  return request<{ message: CommentMessage; conversation: CommentConversation; messages: CommentMessage[] }>(
    `/comments/messages/${commentId}/rerun`,
    { method: 'POST', body: JSON.stringify({}) },
  )
}

/* Settings */
export function getModelSettings() {
  return request<ModelSettings>('/settings/model')
}

export function saveModelSettings(settings: ModelSettingsUpdate) {
  return request<ModelSettings>('/settings/model', {
    method: 'PUT',
    body: JSON.stringify(settings),
  })
}

export function getWorkspaceStatus() {
  return request<WorkspaceStatus>('/settings/workspace')
}

export function getLogStats() {
  return request<LogStats>('/settings/logs')
}

export function clearLogFiles() {
  return request<LogStats>('/settings/logs/clear', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function revealLogFolder() {
  return request<LogRevealResult>('/settings/logs/reveal', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function retryVectorIndex() {
  return request<VectorIndexActionResult>('/settings/vector-index/retry', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function reconcileVectorIndex() {
  return request<VectorIndexActionResult>('/settings/vector-index/reconcile', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

/* ===== Memory workbench (v2 unit/view control surface) ===== */
export type MemoryPortraitPolicy = 'auto' | 'force_include' | 'force_exclude'
export type MemoryPromptPolicy = 'allow' | 'no_prompt'
export type MemoryTier = 'core' | 'contextual' | 'episodic'
export type MemoryViewType = 'user_portrait' | 'soul_relationship_memory'
export type MemoryViewStatus = 'fresh' | 'stale'

/** A row from GET /memory/units (memory_units table). */
export interface MemoryUnit {
  id: string
  owner_scope: string
  visibility_scope: string
  type: string
  content: string
  confidence: number
  importance: number
  tier: MemoryTier
  status: string
  source: string
  source_channel: string
  prompt_policy: MemoryPromptPolicy
  portrait_policy: MemoryPortraitPolicy
  in_portrait: number
}

/** One piece of raw evidence backing a unit. */
export interface MemoryEvidenceRef {
  event_id: number
  source_channel: string
  source_type: string
  source_id: string
  content: string
  occurred_at: number
  author: string | null
  state: string
  review_pending: boolean
}

/** GET /memory/units/{id} — unit plus its evidence trail. */
export interface MemoryUnitDetail {
  unit_id: string
  type: string
  content: string
  confidence: number
  importance: number
  tier: MemoryTier
  status: string
  owner_scope: string
  visibility_scope: string
  source: string
  source_channel: string
  in_portrait: boolean
  prompt_policy: MemoryPromptPolicy
  portrait_policy: MemoryPortraitPolicy
  evidence: MemoryEvidenceRef[]
}

/** A materialized portrait view (top layer of the workbench drill-down). */
export interface MemoryView {
  id: string
  owner_scope: string
  visibility_scope: string
  view_type: MemoryViewType
  content_md: string
  status: MemoryViewStatus
  generated_at?: number | null
  updated_at: number
}

export interface MemoryStatus {
  pending_event_count: number
  pending_buckets: Array<{ owner_scope: string; visibility_scope: string }>
  pending_review_count: number
  pending_relink_count: number
  stale_view_count: number
  active_jobs: Job[]
}

export interface MemoryOperation {
  id: number
  unit_id: string
  related_unit_id: string | null
  op: string
  actor: string
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  reconcile_run_id: number | null
  created_at: number
}

export interface ListMemoryUnitsParams {
  owner_scope?: string
  visibility_scope?: string
  /** 'active' (default) or 'all'. */
  status?: string
  type?: string
  limit?: number
}

export async function listMemoryUnits(params: ListMemoryUnitsParams = {}): Promise<MemoryUnit[]> {
  const query = new URLSearchParams()
  if (params.owner_scope) query.set('owner_scope', params.owner_scope)
  if (params.visibility_scope) query.set('visibility_scope', params.visibility_scope)
  if (params.status) query.set('status', params.status)
  if (params.type) query.set('type', params.type)
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  const data = await request<{ units: MemoryUnit[] }>(`/memory/units${suffix}`)
  return data.units
}

export function getMemoryUnit(unitId: string): Promise<MemoryUnitDetail> {
  return request<MemoryUnitDetail>(`/memory/units/${encodeURIComponent(unitId)}`)
}

export function createMemoryUnit(input: {
  owner_scope?: string
  visibility_scope?: string
  type: string
  content: string
  confidence?: number
  tier?: MemoryTier
  importance?: number
}): Promise<MemoryUnitDetail> {
  return request<MemoryUnitDetail>('/memory/units', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export interface UpdateMemoryUnitInput {
  content: string
  confidence?: number
  type?: string
  tier?: MemoryTier
  importance?: number
}

export function updateMemoryUnit(unitId: string, input: UpdateMemoryUnitInput): Promise<MemoryUnitDetail> {
  return request<MemoryUnitDetail>(`/memory/units/${encodeURIComponent(unitId)}`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  })
}

export type MemoryRetractReason = 'false' | 'outdated'

/** Forget a belief: 'outdated' (was true, no longer — may re-form on new
 *  evidence) or 'false' (misunderstood, never regenerate). To merely stop
 *  mentioning a still-true memory, use setMemoryPromptPolicy('no_prompt'). */
export function retractMemoryUnit(unitId: string, reason: MemoryRetractReason = 'outdated'): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/memory/units/${encodeURIComponent(unitId)}?reason=${reason}`, {
    method: 'DELETE',
  })
}

/** Bring a user-forgotten belief back (「找回」). Only retracted_by_user units qualify. */
export function restoreMemoryUnit(unitId: string): Promise<MemoryUnitDetail> {
  return request<MemoryUnitDetail>(`/memory/units/${encodeURIComponent(unitId)}/restore`, {
    method: 'POST',
  })
}

/** How many live beliefs a source (post/comment/chat message) currently supports. */
export function getMemorySourceImpact(sourceType: string, sourceId: string): Promise<{ count: number }> {
  const params = new URLSearchParams({ source_type: sourceType, source_id: sourceId })
  return request<{ count: number }>(`/memory/source-impact?${params.toString()}`)
}

export function setMemoryPromptPolicy(unitId: string, promptPolicy: MemoryPromptPolicy): Promise<MemoryUnitDetail> {
  return request<MemoryUnitDetail>(`/memory/units/${encodeURIComponent(unitId)}/prompt-policy`, {
    method: 'POST',
    body: JSON.stringify({ prompt_policy: promptPolicy }),
  })
}

export function setMemoryPortraitPolicy(unitId: string, portraitPolicy: MemoryPortraitPolicy): Promise<MemoryUnitDetail> {
  return request<MemoryUnitDetail>(`/memory/units/${encodeURIComponent(unitId)}/portrait-policy`, {
    method: 'POST',
    body: JSON.stringify({ portrait_policy: portraitPolicy }),
  })
}

export async function listMemoryViews(): Promise<MemoryView[]> {
  const data = await request<{ views: MemoryView[] }>('/memory/views')
  return data.views
}

export function resynthesizeMemoryView(input: {
  owner_scope: string
  visibility_scope: string
  view_type: MemoryViewType
}): Promise<MemoryView> {
  return request<MemoryView>('/memory/views/resynthesize', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function getMemoryStatus(): Promise<MemoryStatus> {
  return request<MemoryStatus>('/memory/status')
}

export function triggerMemoryReconcile(): Promise<JobQueued> {
  return request<JobQueued>('/memory/reconcile', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export async function listMemoryOperations(limit = 20): Promise<MemoryOperation[]> {
  const data = await request<{ operations: MemoryOperation[] }>(
    `/memory/operations?limit=${limit}`,
  )
  return data.operations
}

/* ===== Schedule (Outlook / Microsoft Graph) ===== */

/** A goal a schedule event is bound to. */
export interface ScheduleGoalLink {
  goal_id: string
  goal_title: string
}

/** A cached Outlook calendar event (read-only mirror of Graph). */
export interface ScheduleEvent {
  id: string
  subject: string
  body_preview: string | null
  start_ts: number
  end_ts: number
  /** 'YYYY-MM-DDTHH:MM:SS' wall-clock in Asia/Shanghai. */
  start_local: string
  end_local: string
  all_day: boolean
  location: string | null
  web_link: string | null
  series_master_id: string | null
  is_cancelled: boolean
  change_key: string | null
  synced_at: number
  /** 所属日历账号 id（'outlook' | 'local'）。 */
  account_id: string
  /** 账号 provider（'outlook' | 'local'，未来可能有其他云端家）。 */
  provider: string
  goal_link: null
  goal_links: ScheduleGoalLink[]
}

/** A calendar account (local or cloud) that stores schedule events. */
export interface ScheduleAccountInfo {
  id: string
  provider: string
  display_name: string
  event_count: number
}

export interface ScheduleAccount {
  username: string | null
  name: string | null
  home_account_id: string | null
}

export interface ScheduleStatus {
  configured: boolean
  connected: boolean
  account: ScheduleAccount | null
  last_sync_at: number | null
  window_start: string
  window_end: string
  /** 全部日历账号概要（本地 + 云端）。旧 sessionStorage 快照可能缺失。 */
  accounts?: ScheduleAccountInfo[]
  /** 是否应弹出「本地→Outlook 迁移」一次性邀请（已连接 且 有本地日程 且未 dismiss）。 */
  migration_prompt_pending?: boolean
}

/** 迁移预检 / 冲突对照里的单个事件摘要（本地或 Outlook 现有）。 */
export interface MigrationEventRef {
  id: string
  subject: string
  /** 'YYYY-MM-DDTHH:MM:SS' wall-clock in Asia/Shanghai. */
  start_local: string
  end_local: string
  all_day: boolean
}

/** 一处迁移冲突：本地事件与 Outlook 中疑似已存在的事件对照。 */
export interface MigrationConflict {
  local: MigrationEventRef
  existing: MigrationEventRef
}

/** 迁移预检结果：总数、无冲突可直接迁入数、逐条冲突。 */
export interface MigrationPreview {
  total: number
  clean: number
  conflicts: MigrationConflict[]
}

/** 迁移执行结果。partial 表示中途失败、尚有剩余可重试。 */
export interface MigrationResult {
  status: 'ok' | 'partial'
  migrated: number
  skipped: number
  remaining: number
  account_removed: boolean
  error?: string
}

export interface ScheduleClientIdInfo {
  configured: boolean
  /** True when the built-in shared app is in use (no custom client_id saved). */
  using_default: boolean
  client_id_tail: string | null
}

export interface ScheduleDeviceStart {
  user_code: string
  verification_uri: string
  expires_in: number | null
}

/** Shared poll result for both interactive and device-code sign-in flows. */
export interface ScheduleAuthStatus {
  status: 'pending' | 'ok' | 'error'
  account?: ScheduleAccount
  error?: string
  /** Present on interactive-login errors: suggests falling back to device code. */
  fallback?: 'device_code'
}

export interface ScheduleSyncResult {
  ok: boolean
  configured: boolean
  connected: boolean
  status: string
  upserted: number
  deleted: number
  last_sync_at: number | null
}

export interface ScheduleEventsResult {
  events: ScheduleEvent[]
  configured: boolean
  connected: boolean
}

export interface CreateScheduleEventInput {
  subject: string
  /** 'YYYY-MM-DD' */
  date: string
  /** 'HH:MM' — ignored when all_day. */
  start_time?: string
  end_time?: string
  all_day?: boolean
  goal_id?: string
  /** 保存到的账号（'outlook' | 'local'）；缺省由后端路由（已连接→Outlook，否则本地）。 */
  account_id?: string
  /** 同一次创建及其重试复用的幂等请求号，避免 Outlook 重复创建。 */
  client_request_id?: string
}

/** A goal's weekly schedule expectation ({ period, target, label }). */
export interface ScheduleExpectation {
  period: 'week'
  target: number
  label: string
}

/** Weekly progress against a goal's schedule expectation. */
export interface ScheduleProgress {
  goal_id: string
  week_start: string
  week_end: string
  current: number
  target: number | null
  text: string | null
  expectation: ScheduleExpectation | null
}

export interface GoalSchedule {
  events: ScheduleEvent[]
  progress: ScheduleProgress
}

export interface PostActivity {
  id: string
  ts: string
}

export function getScheduleStatus() {
  return request<ScheduleStatus>('/schedule/status')
}

export function getScheduleClientId() {
  return request<ScheduleClientIdInfo>('/schedule/auth/client-id')
}

export function saveScheduleClientId(clientId: string) {
  return request<ScheduleClientIdInfo>('/schedule/auth/client-id', {
    method: 'POST',
    body: JSON.stringify({ client_id: clientId }),
  })
}

/** Restore the built-in shared app (clears any custom client_id + login state). */
export function restoreDefaultClientId() {
  return request<ScheduleClientIdInfo>('/schedule/auth/client-id', {
    method: 'DELETE',
  })
}

/** Start the one-click browser (interactive) sign-in; poll getAuthStatus after. */
export function startInteractiveAuth() {
  return request<{ status: string }>('/schedule/auth/interactive-start', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function startScheduleDeviceLogin() {
  return request<ScheduleDeviceStart>('/schedule/auth/device-start', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

/** Poll the in-progress sign-in flow (interactive or device code). */
export function getAuthStatus() {
  return request<ScheduleAuthStatus>('/schedule/auth/status')
}

export function scheduleLogout() {
  return request<{ ok: boolean }>('/schedule/auth/logout', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

/** List cached events in [start, end] (inclusive, 'YYYY-MM-DD'). The route
 *  reports connection state via headers, not the body. */
export async function listScheduleEvents(start: string, end: string): Promise<ScheduleEventsResult> {
  const res = await fetch(`${BASE}/schedule/events?start=${start}&end=${end}`, {
    headers: { 'Content-Type': 'application/json' },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new ApiError(body.detail || `HTTP ${res.status}`, res.status)
  }
  const events = (await res.json()) as ScheduleEvent[]
  return {
    events,
    configured: res.headers.get('X-Schedule-Configured') === 'true',
    connected: res.headers.get('X-Schedule-Connected') === 'true',
  }
}

export function createScheduleEvent(input: CreateScheduleEventInput) {
  return request<ScheduleEvent>('/schedule/events', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

export function updateScheduleEvent(eventId: string, changes: Partial<CreateScheduleEventInput>) {
  return request<ScheduleEvent>(`/schedule/events/${encodeURIComponent(eventId)}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  })
}

export function deleteScheduleEvent(eventId: string) {
  return request<{ ok: boolean }>(`/schedule/events/${encodeURIComponent(eventId)}`, {
    method: 'DELETE',
  })
}

export function listScheduleAccounts() {
  return request<ScheduleAccountInfo[]>('/schedule/accounts')
}

export function createLocalCalendarAccount() {
  return request<ScheduleAccountInfo>('/schedule/accounts/local', { method: 'POST' })
}

export function deleteLocalCalendarAccount(deleteEvents: boolean) {
  return request<{ ok: boolean; deleted_events: number }>('/schedule/accounts/local', {
    method: 'DELETE',
    body: JSON.stringify({ delete_events: deleteEvents }),
  })
}

/** 预检本地→Outlook 迁移（有 sync 副作用故用 POST）。未连接 / 无本地账号 / 0 条 → 409。 */
export function previewLocalMigration() {
  return request<MigrationPreview>('/schedule/accounts/local/migration/preview', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

/** 执行迁移。decisions 仅对冲突项有意义（'skip' | 'create'），缺省按 'skip'。 */
export function migrateLocalEvents(decisions: Record<string, 'skip' | 'create'>) {
  return request<MigrationResult>('/schedule/accounts/local/migration', {
    method: 'POST',
    body: JSON.stringify({ decisions }),
  })
}

/** 抑制一次性迁移邀请弹窗（「暂不」永不再自动弹）。 */
export function dismissMigrationPrompt() {
  return request<{ ok: boolean }>('/schedule/accounts/local/migration/dismiss', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function syncSchedule() {
  return request<ScheduleSyncResult>('/schedule/sync', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function listPostActivity(start: string, end: string) {
  return request<PostActivity[]>(`/posts/activity?start=${start}&end=${end}`)
}

/* ===== Goal ↔ schedule links ===== */

export function getGoalSchedule(goalId: string) {
  return request<GoalSchedule>(`/goals/${encodeURIComponent(goalId)}/schedule`)
}

export function linkGoalSchedule(goalId: string, eventId: string) {
  return request<{ goal_id: string; event_id: string; created_at: number }>(
    `/goals/${encodeURIComponent(goalId)}/schedule/links`,
    { method: 'POST', body: JSON.stringify({ event_id: eventId }) },
  )
}

export function unlinkGoalSchedule(goalId: string, eventId: string) {
  return request<{ ok: boolean }>(
    `/goals/${encodeURIComponent(goalId)}/schedule/links/${encodeURIComponent(eventId)}`,
    { method: 'DELETE' },
  )
}

export function updateGoalScheduleExpectation(goalId: string, expectation: ScheduleExpectation) {
  return request<{ expectation: ScheduleExpectation | null; progress: ScheduleProgress }>(
    `/goals/${encodeURIComponent(goalId)}/schedule/expectation`,
    { method: 'PUT', body: JSON.stringify(expectation) },
  )
}

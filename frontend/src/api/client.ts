/* API client for TraceLog backend */

const BASE = '/api'

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new ApiError(body.detail || `HTTP ${res.status}`, res.status)
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

export interface Todo {
  id: string
  task: string
  date: string | null
  start_time: string | null
  end_time: string | null
  status: string
  source_post: string | null
  created_at?: number
  updated_at?: number
  completed_at?: number | null
}

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
  | 'todo_started'
  | 'todo_succeeded'
  | 'todo_failed'
  | 'light_reflection_started'
  | 'light_reflection_succeeded'
  | 'light_reflection_failed'
  | 'deep_reflection_queued'
  | 'deep_reflection_succeeded'
  | 'deep_reflection_failed'
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
}

export interface CommentReplyResult {
  post_id: string
  soul_name: string
  ok: boolean
  reply: string
  user_message_id: number
  assistant_message_id: number | null
  error: string | null
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

export interface ReflectionScope {
  post_ids: string[]
  scope_start: string | null
  scope_end: string | null
}

export interface SoulReflectionScope {
  soul_name: string
  interaction_count: number
  scope_start: number
  scope_end: number
}

export interface MemoryRevisionSummary {
  id: number
  target_type: 'user' | 'soul'
  target_name: string | null
  source: string
  patch: unknown
  created_at: number
}

export interface MemoryRevisionDetail extends MemoryRevisionSummary {
  snapshot: string
}

export interface JobQueued {
  job_id: number
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
  logging: {
    enabled: boolean
    level: string
    history_retention: number
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
  soul_memories_dir: string
  user_memory_path: string
  counts: {
    posts: number
    comments: number
    souls: number
    enabled_souls: number
    todos: number
    jobs: number
    vision_cache: number
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
  'todo_started',
  'todo_succeeded',
  'todo_failed',
  'light_reflection_started',
  'light_reflection_succeeded',
  'light_reflection_failed',
  'deep_reflection_queued',
  'deep_reflection_succeeded',
  'deep_reflection_failed',
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

export function generateSoul(name: string, inspiration: string) {
  return request<{ soul: string }>('/souls/generate-soul', {
    method: 'POST',
    body: JSON.stringify({ name, inspiration }),
  })
}

export function updateSoul(name: string, changes: { enabled?: boolean; description?: string }) {
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

/* Profile */
export function getProfile() {
  return request<{ content: string }>('/profile')
}

export function updateProfile(content: string) {
  return request<{ ok: boolean; content: string }>('/profile', {
    method: 'PUT',
    body: JSON.stringify({ content }),
  })
}

export function listProfileRevisions(limit = 10) {
  return request<MemoryRevisionSummary[]>(`/profile/revisions?limit=${limit}`)
}

export function getProfileRevision(revisionId: number) {
  return request<MemoryRevisionDetail>(`/profile/revisions/${revisionId}`)
}

export function getSoulMemory(name: string) {
  return request<{ soul_name: string; content: string }>(`/souls/${encodeURIComponent(name)}/memory`)
}

export function updateSoulMemory(name: string, content: string) {
  return request<{ soul_name: string; content: string }>(`/souls/${encodeURIComponent(name)}/memory`, {
    method: 'PUT',
    body: JSON.stringify({ content }),
  })
}

export function listSoulMemoryRevisions(name: string, limit = 5) {
  return request<MemoryRevisionSummary[]>(
    `/souls/${encodeURIComponent(name)}/memory/revisions?limit=${limit}`,
  )
}

export function getSoulMemoryRevision(name: string, revisionId: number) {
  return request<MemoryRevisionDetail>(
    `/souls/${encodeURIComponent(name)}/memory/revisions/${revisionId}`,
  )
}

/* Todos */
export function listTodos() {
  return request<Todo[]>('/todos')
}

export function createTodo(changes: Partial<Todo> & { task: string }) {
  return request<Todo>('/todos', {
    method: 'POST',
    body: JSON.stringify(changes),
  })
}

export function updateTodo(todoId: string, changes: Partial<Todo>) {
  return request<Todo>(`/todos/${todoId}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  })
}

export function deleteTodo(todoId: string) {
  return request<{ ok: boolean }>(`/todos/${todoId}`, {
    method: 'DELETE',
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

export function sendChatMessage(soulName: string, content: string, attachmentIds: string[] = []) {
  return request<{ thread: ChatThread; result: ChatReplyResult; messages: ChatMessage[] }>(
    `/chat/${soulName}/messages`,
    { method: 'POST', body: JSON.stringify({ content, attachment_ids: attachmentIds }) },
  )
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

/* Reflections */
const DEFAULT_REFLECTION_LIMIT = 100

export function previewGlobalReflection(limit = DEFAULT_REFLECTION_LIMIT) {
  return request<ReflectionScope>(`/reflections/global/preview?limit=${limit}`)
}

export function triggerGlobalReflection(limit = DEFAULT_REFLECTION_LIMIT) {
  return request<JobQueued>('/reflections/global', {
    method: 'POST',
    body: JSON.stringify({ limit }),
  })
}

export function previewSoulReflections(limitPerSoul = DEFAULT_REFLECTION_LIMIT) {
  return request<SoulReflectionScope[]>(
    `/reflections/souls/preview?limit_per_soul=${limitPerSoul}`,
  )
}

export function triggerSoulReflections(limitPerSoul = DEFAULT_REFLECTION_LIMIT) {
  return request<JobQueued>('/reflections/souls', {
    method: 'POST',
    body: JSON.stringify({ limit_per_soul: limitPerSoul }),
  })
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

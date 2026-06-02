/* API client for TraceLog backend */

const BASE = '/api'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
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
}

export interface PostDetail {
  post: {
    post_id: string
    ts: string
    content: string
    importance: number
    created_at: number
    updated_at: number
  }
  comments: Comment[]
  jobs: unknown[]
  events: unknown[]
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

export interface JobQueued {
  job_id: number
  status: string
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
  job_worker_concurrency: number
  logging: {
    enabled: boolean
    level: string
    history_retention: number
  }
  config_path: string
  restart_required?: boolean
}

export interface ModelSettingsUpdate {
  api_key?: string
  base_url: string
  model: string
  embedding_model: string
  embedding_api_key?: string
  embedding_base_url?: string | null
  reuse_embedding_config: boolean
  job_worker_concurrency: number
  logging: ModelSettings['logging']
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
  }
  logs: {
    current_log_path: string
    current_log_exists: boolean
    current_log_size_bytes: number
    history_dir: string
    history_count: number
  }
}

/* Posts */
export function listPosts(limit = 20, offset = 0) {
  return request<Post[]>(`/posts?limit=${limit}&offset=${offset}`)
}

export function getPost(postId: string) {
  return request<PostDetail>(`/posts/${postId}`)
}

export function createPost(content: string) {
  return request<{ post_id: string; status: string; job_ids: number[] }>(
    '/posts',
    { method: 'POST', body: JSON.stringify({ content }) },
  )
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
): () => void {
  const es = new EventSource(`${BASE}/posts/${postId}/events`)

  POST_EVENT_TYPES.forEach((type) => {
    es.addEventListener(type, (e) => {
      const data = JSON.parse(e.data) as PostEvent
      onEvent(data)
      if (data.event_type === 'pipeline_done') {
        onDone?.()
        es.close()
      }
    })
  })

  es.onerror = () => {
    es.close()
  }

  return () => es.close()
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

/* Todos */
export function listTodos() {
  return request<Todo[]>('/todos')
}

export function updateTodo(todoId: string, changes: Partial<Todo>) {
  return request<Todo>(`/todos/${todoId}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  })
}

/* Chat */
export function listChatThreads(soulName: string) {
  return request<ChatThread[]>(`/chat/${soulName}/threads`)
}

export function getChatThread(threadId: number, limit = 30) {
  return request<{ thread: ChatThread; messages: ChatMessage[] }>(
    `/chat/threads/${threadId}?limit=${limit}`,
  )
}

export function sendChatMessage(soulName: string, content: string) {
  return request<{ thread: ChatThread; result: ChatReplyResult; messages: ChatMessage[] }>(
    `/chat/${soulName}/messages`,
    { method: 'POST', body: JSON.stringify({ content }) },
  )
}

/* Reflections */
export function previewGlobalReflection(limit = 100) {
  return request<ReflectionScope>(`/reflections/global/preview?limit=${limit}`)
}

export function triggerGlobalReflection(limit = 100) {
  return request<JobQueued>('/reflections/global', {
    method: 'POST',
    body: JSON.stringify({ limit }),
  })
}

export function previewSoulReflections(limitPerSoul = 100) {
  return request<SoulReflectionScope[]>(
    `/reflections/souls/preview?limit_per_soul=${limitPerSoul}`,
  )
}

export function triggerSoulReflections(limitPerSoul = 100) {
  return request<JobQueued>('/reflections/souls', {
    method: 'POST',
    body: JSON.stringify({ limit_per_soul: limitPerSoul }),
  })
}

/* Comment conversations */
export function listCommentConversations(postId: string) {
  return request<CommentConversation[]>(
    `/comments/posts/${encodeURIComponent(postId)}/conversations`,
  )
}

export function getCommentConversation(postId: string, soulName: string, limit = 30) {
  return request<{ conversation: CommentConversation; messages: CommentMessage[] }>(
    `/comments/posts/${encodeURIComponent(postId)}/souls/${encodeURIComponent(soulName)}?limit=${limit}`,
  )
}

export function sendCommentMessage(postId: string, soulName: string, content: string) {
  return request<{
    conversation: CommentConversation
    result: CommentReplyResult
    messages: CommentMessage[]
  }>(
    `/comments/posts/${encodeURIComponent(postId)}/souls/${encodeURIComponent(soulName)}/messages`,
    { method: 'POST', body: JSON.stringify({ content }) },
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

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
  content: string
  is_main: number
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

export interface CommentThread {
  id: number
  post_id: string
  soul_name: string
  root_comment_id: number
  created_at: number
  updated_at: number
  last_message_at: number | null
}

export interface CommentMessage {
  id: number
  thread_id: number
  role: string
  content: string
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
  thread_id: number
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

/* Comment threads */
export function listCommentThreads(postId: string) {
  return request<CommentThread[]>(`/comments/posts/${encodeURIComponent(postId)}/threads`)
}

export function getCommentThread(threadId: number, limit = 30) {
  return request<{ thread: CommentThread; messages: CommentMessage[] }>(
    `/comments/threads/${threadId}?limit=${limit}`,
  )
}

export function sendCommentMessage(postId: string, soulName: string, content: string) {
  return request<{
    thread: CommentThread
    result: CommentReplyResult
    messages: CommentMessage[]
  }>(
    `/comments/${encodeURIComponent(postId)}/${encodeURIComponent(soulName)}/messages`,
    { method: 'POST', body: JSON.stringify({ content }) },
  )
}

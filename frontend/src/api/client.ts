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
}

export interface ChatThread {
  id: number
  soul_name: string
  created_at: number
  updated_at: number
  message_count: number
}

export interface ChatMessage {
  id: number
  thread_id: number
  role: string
  content: string
  created_at: number
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
export function streamPostEvents(
  postId: string,
  onEvent: (event: { event_type: string; payload: unknown }) => void,
  onDone?: () => void,
): () => void {
  const es = new EventSource(`${BASE}/posts/${postId}/events`)

  es.addEventListener('pipeline_done', (e) => {
    const data = JSON.parse(e.data)
    onEvent(data)
    onDone?.()
    es.close()
  })

  es.addEventListener('comment_generated', (e) => {
    onEvent(JSON.parse(e.data))
  })

  es.addEventListener('todo_extracted', (e) => {
    onEvent(JSON.parse(e.data))
  })

  es.addEventListener('light_reflection_done', (e) => {
    onEvent(JSON.parse(e.data))
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
  return request<{ thread: ChatThread; result: unknown; messages: ChatMessage[] }>(
    `/chat/${soulName}/messages`,
    { method: 'POST', body: JSON.stringify({ content }) },
  )
}

/* Comment threads */
export function listCommentThreads(postId: string) {
  return request<unknown[]>(`/comments/posts/${postId}/threads`)
}

export function sendCommentMessage(postId: string, soulName: string, content: string) {
  return request<unknown>(
    `/comments/${postId}/${soulName}/messages`,
    { method: 'POST', body: JSON.stringify({ content }) },
  )
}

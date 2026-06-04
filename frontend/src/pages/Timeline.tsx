import { useCallback, useEffect, useState } from 'react'
import {
  type Attachment,
  type Comment,
  type CommentConversation,
  type CommentMessage,
  type Post,
  type PostEvent,
  createPost,
  getCommentConversation,
  getPost,
  listCommentConversations,
  listPosts,
  sendCommentMessage,
  streamPostEvents,
} from '@/api/client'
import { Composer } from '@/components/Composer'
import { type CommentConversationState, PostCard } from '@/components/PostCard'
import { API_LIMITS } from '@/utils/constants'
import styles from './Timeline.module.css'

interface TimelineProps {
  onActivitySettled?: () => void
  onTodosChanged?: () => void
}

export function Timeline({ onActivitySettled, onTodosChanged }: TimelineProps) {
  const [posts, setPosts] = useState<Post[]>([])
  const [postComments, setPostComments] = useState<Record<string, Comment[]>>({})
  const [postCommentConversations, setPostCommentConversations] = useState<Record<string, Record<string, CommentConversationState>>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchPosts = useCallback(async () => {
    try {
      const data = await listPosts(API_LIMITS.POSTS_DEFAULT, 0)
      setPosts(data)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchPosts()
  }, [fetchPosts])

  const handleSubmit = async (content: string, attachments: Attachment[]) => {
    const result = await createPost(content, attachments.map((attachment) => attachment.id))
    /* Optimistically add the post to the top */
    const newPost: Post = {
      post_id: result.post_id,
      ts: new Date().toISOString(),
      content,
      importance: 0.5,
      comment_count: 0,
      latest_event_type: 'queued',
      attachments,
    }
    setPosts((prev) => [newPost, ...prev])

    /* Subscribe to SSE for real-time comment updates */
    streamPostEvents(
      result.post_id,
      (event) => {
        setPosts((prev) =>
          prev.map((p) =>
            p.post_id === result.post_id
              ? { ...p, latest_event_type: event.event_type }
              : p,
          ),
        )

        if (shouldRefreshPostDetail(event)) {
          refreshPostDetail(result.post_id, event.event_type)
        }

        if (event.event_type === 'todo_succeeded') {
          onTodosChanged?.()
        }
      },
      () => {
        /* Pipeline done — mark post as complete */
        setPosts((prev) =>
          prev.map((p) =>
            p.post_id === result.post_id
              ? { ...p, latest_event_type: 'pipeline_done' }
              : p,
          ),
        )
        onActivitySettled?.()
      },
    )
  }

  const refreshPostDetail = async (postId: string, eventType?: string) => {
    try {
      const detail = await getPost(postId)
      setPostComments((prev) => ({
        ...prev,
        [postId]: detail.comments,
      }))
      await refreshCommentConversations(postId)
      setPosts((prev) =>
        prev.map((p) =>
          p.post_id === postId
            ? {
                ...p,
                importance: detail.post.importance,
                comment_count: detail.comments.length,
                latest_event_type: eventType ?? p.latest_event_type,
                attachments: detail.post.attachments,
              }
            : p,
        ),
      )
    } catch {
      /* keep the optimistic post visible if detail refresh fails */
    }
  }

  const handleExpand = async (postId: string) => {
    try {
      const detail = await getPost(postId)
      setPostComments((prev) => ({ ...prev, [postId]: detail.comments }))
      await refreshCommentConversations(postId)
    } catch {
      /* silently fail for now */
    }
  }

  const refreshCommentConversations = async (postId: string) => {
    try {
      const conversations = await listCommentConversations(postId)
      const details = await Promise.all(
        conversations.map(async (conversation) => {
          const detail = await getCommentConversation(postId, conversation.soul_name)
          return [conversation.soul_name, toConversationState(detail.conversation, detail.messages)] as const
        }),
      )
      setPostCommentConversations((prev) => ({
        ...prev,
        [postId]: Object.fromEntries(details),
      }))
    } catch {
      /* Keep root comments visible even if thread history is unavailable. */
    }
  }

  const handleCommentReply = async (postId: string, soulName: string, content: string, attachments: Attachment[]) => {
    const optimisticMessage: CommentMessage = {
      id: -Date.now(),
      post_id: postId,
      soul_name: soulName,
      role: 'user',
      content,
      seq: Number.MAX_SAFE_INTEGER,
      created_at: Date.now() / 1000,
      attachments,
    }
    setPostCommentConversations((prev) => ({
      ...prev,
      [postId]: {
        ...(prev[postId] ?? {}),
        [soulName]: {
          ...(prev[postId]?.[soulName] ?? { messages: [] }),
          messages: [...(prev[postId]?.[soulName]?.messages ?? []), optimisticMessage],
          sending: true,
          error: null,
        },
      },
    }))

    try {
      const response = await sendCommentMessage(postId, soulName, content, attachments.map((attachment) => attachment.id))
      setPostCommentConversations((prev) => ({
        ...prev,
        [postId]: {
          ...(prev[postId] ?? {}),
          [soulName]: toConversationState(response.conversation, response.messages),
        },
      }))
    } catch (err) {
      setPostCommentConversations((prev) => ({
        ...prev,
        [postId]: {
          ...(prev[postId] ?? {}),
          [soulName]: {
            ...(prev[postId]?.[soulName] ?? { messages: [] }),
            sending: false,
            error: err instanceof Error ? err.message : '发送失败',
          },
        },
      }))
    }
  }

  if (loading) {
    return (
      <div className={styles.timeline}>
        <TimelineHeader />
        <div className={styles.loading}>
          <div className={styles.skeleton} />
          <div className={styles.skeleton} />
          <div className={styles.skeleton} />
        </div>
      </div>
    )
  }

  return (
    <div className={styles.timeline}>
      <TimelineHeader />
      <Composer onSubmit={handleSubmit} />

      {error ? (
        <div className={styles.error}>
          <p>无法加载时间线</p>
          <p className={styles.errorDetail}>{error}</p>
          <button className={styles.retryBtn} onClick={fetchPosts}>重试</button>
        </div>
      ) : posts.length === 0 ? (
        <div className={styles.empty}>
          <EmptyIcon />
          <p className={styles.emptyTitle}>还没有记录</p>
          <p className={styles.emptyHint}>写下你的第一条想法，人格会回应你</p>
        </div>
      ) : (
        <div className={styles.feed}>
          {posts.map((post) => (
            <PostCard
              key={post.post_id}
              post={post}
              comments={postComments[post.post_id]}
              commentConversations={postCommentConversations[post.post_id]}
              onExpand={() => handleExpand(post.post_id)}
              onReply={(soulName, content, attachments) => handleCommentReply(post.post_id, soulName, content, attachments)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function toConversationState(conversation: CommentConversation, messages: CommentMessage[]): CommentConversationState {
  return {
    conversation,
    messages,
    sending: false,
    error: null,
  }
}

function TimelineHeader() {
  return (
    <header className={styles.header}>
      <div>
        <h1>首页</h1>
        <p>记录、回应、反思都流回这里</p>
      </div>
    </header>
  )
}

function shouldRefreshPostDetail(event: PostEvent): boolean {
  return [
    'reply_succeeded',
    'reply_failed',
    'light_reflection_succeeded',
    'pipeline_done',
  ].includes(event.event_type)
}

function EmptyIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity="0.3">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
    </svg>
  )
}

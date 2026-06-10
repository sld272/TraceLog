import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type Attachment,
  type Comment,
  type CommentConversation,
  type CommentMessage,
  type Post,
  type PostEvent,
  createPost,
  deleteCommentMessage,
  deletePost,
  getCommentConversation,
  getPost,
  listCommentConversations,
  listPosts,
  retryJob,
  sendCommentMessage,
  streamPostEvents,
  rerunCommentMessage,
} from '@/api/client'
import { Composer } from '@/components/Composer'
import { ConfirmDialog } from '@/components/ConfirmDialog'
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
  const [deletingPostId, setDeletingPostId] = useState<string | null>(null)
  const [busyCommentId, setBusyCommentId] = useState<number | null>(null)
  const [retryingJobId, setRetryingJobId] = useState<number | null>(null)
  const [regeneratedCommentId, setRegeneratedCommentId] = useState<number | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean
    title: string
    message: string
    onConfirm: () => void
  } | null>(null)
  const regeneratedCommentTimerRef = useRef<number | null>(null)
  const retryPollTokenRef = useRef(0)

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

  useEffect(() => {
    return () => {
      if (regeneratedCommentTimerRef.current !== null) {
        window.clearTimeout(regeneratedCommentTimerRef.current)
      }
      retryPollTokenRef.current += 1
    }
  }, [])

  const handleSubmit = async (content: string, attachments: Attachment[]) => {
    let result
    try {
      result = await createPost(content, attachments.map((attachment) => attachment.id))
      setError(null)
    } catch (err) {
      const message = err instanceof Error ? err.message : '发布失败'
      setError(message)
      throw new Error(message)
    }
    /* Optimistically add the post to the top */
    const newPost: Post = {
      post_id: result.post_id,
      ts: new Date().toISOString(),
      content,
      importance: 0.5,
      comment_count: 0,
      latest_event_type: 'queued',
      pipeline_status: {
        state: 'running',
        pending_count: result.job_ids.length,
        running_count: 0,
        retrying_count: 0,
        failed_jobs: [],
      },
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
                latest_event_type: detail.post.latest_event_type ?? eventType ?? p.latest_event_type,
                pipeline_status: detail.post.pipeline_status,
                attachments: detail.post.attachments,
              }
            : p,
        ),
      )
      return detail
    } catch {
      /* keep the optimistic post visible if detail refresh fails */
      return null
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
    const optimisticUserId = -Date.now()
    const optimisticAssistantId = optimisticUserId - 1
    setPostCommentConversations((prev) => ({
      ...prev,
      [postId]: {
        ...(prev[postId] ?? {}),
        [soulName]: buildSendingCommentState(
          prev[postId]?.[soulName],
          postId,
          soulName,
          content,
          attachments,
          optimisticUserId,
          optimisticAssistantId,
        ),
      },
    }))

    try {
      const response = await sendCommentMessage(postId, soulName, content, attachments.map((attachment) => attachment.id))
      setPostCommentConversations((prev) => ({
        ...prev,
        [postId]: {
          ...(prev[postId] ?? {}),
          [soulName]: response.result.ok
            ? toConversationState(response.conversation, response.messages)
            : failedCommentState(response.conversation, response.messages, response.result.error),
        },
      }))
    } catch (err) {
      setPostCommentConversations((prev) => ({
        ...prev,
        [postId]: {
          ...(prev[postId] ?? {}),
          [soulName]: {
            ...(prev[postId]?.[soulName] ?? { messages: [] }),
            messages: (prev[postId]?.[soulName]?.messages ?? []).filter(
              (message) => message.id !== optimisticUserId && message.id !== optimisticAssistantId,
            ),
            sending: false,
            error: err instanceof Error ? err.message : '发送失败',
          },
        },
      }))
      throw err
    }
  }

  const handleDeletePost = async (postId: string) => {
    setConfirmDialog({
      isOpen: true,
      title: '删除 Post',
      message: '删除这条 post 会同时删除所有 SOUL 回复和评论对话，且不会自动恢复。确定删除吗？',
      onConfirm: async () => {
        setConfirmDialog(null)
        setDeletingPostId(postId)
        try {
          await deletePost(postId)
          setPosts((prev) => prev.filter((post) => post.post_id !== postId))
          setPostComments((prev) => {
            const next = { ...prev }
            delete next[postId]
            return next
          })
          setPostCommentConversations((prev) => {
            const next = { ...prev }
            delete next[postId]
            return next
          })
          onTodosChanged?.()
          onActivitySettled?.()
        } catch (err) {
          setError(err instanceof Error ? err.message : '删除失败')
        } finally {
          setDeletingPostId(null)
        }
      },
    })
  }

  const handleDeleteComment = async (postId: string, commentId: number) => {
    setConfirmDialog({
      isOpen: true,
      title: '删除评论',
      message: '删除这条评论会同时删除它之后的同一段对话，且不会自动恢复。确定删除吗？',
      onConfirm: async () => {
        setConfirmDialog(null)
        setBusyCommentId(commentId)
        try {
          await deleteCommentMessage(commentId)
          await refreshPostDetail(postId)
        } catch (err) {
          setError(err instanceof Error ? err.message : '删除评论失败')
        } finally {
          setBusyCommentId(null)
        }
      },
    })
  }

  const handleRerunComment = async (postId: string, commentId: number) => {
    const previousConversations = postCommentConversations
    setBusyCommentId(commentId)
    setPostCommentConversations((prev) =>
      withPendingCommentRerun(prev, postId, commentId, postComments[postId] ?? []),
    )
    try {
      const response = await rerunCommentMessage(commentId)
      // Update the conversation state with the returned data
      setPostCommentConversations((prev) => ({
        ...prev,
        [postId]: {
          ...(prev[postId] ?? {}),
          [response.conversation.soul_name]: toConversationState(response.conversation, response.messages),
        },
      }))
      // Also refresh post detail to get updated comment list
      await refreshPostDetail(postId)
      showRegeneratedComment(response.message.id)
    } catch {
      setPostCommentConversations(previousConversations)
      await refreshPostDetail(postId)
    } finally {
      setBusyCommentId(null)
    }
  }

  const handleRetryPostJobs = async (postId: string, jobIds: number[]) => {
    const firstJobId = jobIds[0]
    if (firstJobId === undefined) return
    setRetryingJobId(firstJobId)
    setError(null)
    try {
      const beforeRetry = await getPost(postId)
      const afterEventId = latestEventId(beforeRetry.events)
      await Promise.all(jobIds.map((jobId) => retryJob(jobId)))
      await refreshPostDetail(postId)
      streamPostEvents(
        postId,
        (event) => {
          setPosts((prev) =>
            prev.map((p) =>
              p.post_id === postId
                ? { ...p, latest_event_type: event.event_type }
                : p,
            ),
          )
          if (shouldRefreshPostDetail(event)) {
            refreshPostDetail(postId, event.event_type)
          }
          if (event.event_type === 'todo_succeeded') {
            onTodosChanged?.()
          }
        },
        () => {
          setPosts((prev) =>
            prev.map((p) =>
              p.post_id === postId
                ? { ...p, latest_event_type: 'pipeline_done' }
                : p,
            ),
          )
          refreshPostDetail(postId)
          onActivitySettled?.()
        },
        { afterEventId },
      )
      pollPostPipelineUntilSettled(postId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '重试失败')
    } finally {
      setRetryingJobId(null)
    }
  }

  const pollPostPipelineUntilSettled = async (postId: string) => {
    const token = retryPollTokenRef.current + 1
    retryPollTokenRef.current = token
    const deadline = Date.now() + 30_000
    while (Date.now() < deadline && retryPollTokenRef.current === token) {
      await sleep(3_000)
      if (retryPollTokenRef.current !== token) return
      const detail = await refreshPostDetail(postId)
      const state = detail?.post.pipeline_status?.state
      if (state === 'failed' || state === 'done' || state === 'idle') {
        onActivitySettled?.()
        return
      }
    }
    if (retryPollTokenRef.current === token) {
      await refreshPostDetail(postId)
    }
  }

  const showRegeneratedComment = (commentId: number) => {
    if (regeneratedCommentTimerRef.current !== null) {
      window.clearTimeout(regeneratedCommentTimerRef.current)
    }
    setRegeneratedCommentId(commentId)
    regeneratedCommentTimerRef.current = window.setTimeout(() => {
      setRegeneratedCommentId(null)
      regeneratedCommentTimerRef.current = null
    }, 3000)
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

      {error && posts.length === 0 ? (
        <div className={styles.error}>
          <p>无法加载时间线</p>
          <p className={styles.errorDetail}>{error}</p>
          <button className={styles.retryBtn} onClick={fetchPosts}>重试</button>
        </div>
      ) : (
        <>
          {error && (
            <div className={styles.inlineError} role="alert">
              {error}
            </div>
          )}
          {posts.length === 0 ? (
            <div className={styles.empty}>
              <EmptyIcon />
              <p className={styles.emptyTitle}>还没有记录</p>
              <p className={styles.emptyHint}>写下你的第一条想法，TA 们会回应你</p>
            </div>
          ) : (
            <div className={styles.feed}>
              {posts.map((post) => (
                <PostCard
                  key={post.post_id}
                  post={post}
                  comments={postComments[post.post_id]}
                  commentConversations={postCommentConversations[post.post_id]}
                  busyCommentId={busyCommentId}
                  regeneratedCommentId={regeneratedCommentId}
                  deletingPost={deletingPostId === post.post_id}
                  retryingJobId={retryingJobId}
                  onExpand={() => handleExpand(post.post_id)}
                  onReply={(soulName, content, attachments) => handleCommentReply(post.post_id, soulName, content, attachments)}
                  onDeletePost={() => handleDeletePost(post.post_id)}
                  onDeleteComment={(commentId) => handleDeleteComment(post.post_id, commentId)}
                  onRerunComment={(commentId) => handleRerunComment(post.post_id, commentId)}
                  onRetryFailedJobs={(jobIds) => handleRetryPostJobs(post.post_id, jobIds)}
                />
              ))}
            </div>
          )}
        </>
      )}

      {confirmDialog && (
        <ConfirmDialog
          isOpen={confirmDialog.isOpen}
          title={confirmDialog.title}
          message={confirmDialog.message}
          confirmText="删除"
          cancelText="取消"
          danger
          onConfirm={confirmDialog.onConfirm}
          onCancel={() => setConfirmDialog(null)}
        />
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

function failedCommentState(
  conversation: CommentConversation,
  messages: CommentMessage[],
  error: string | null,
): CommentConversationState {
  return {
    conversation,
    messages,
    sending: false,
    error: error && messages.length === 0 ? error : null,
  }
}

function buildSendingCommentState(
  current: CommentConversationState | undefined,
  postId: string,
  soulName: string,
  content: string,
  attachments: Attachment[],
  optimisticUserId: number,
  optimisticAssistantId: number,
): CommentConversationState {
  const messages = current?.messages ?? []
  const nextSeq = Math.max(0, ...messages.map((message) => message.seq)) + 1
  const createdAt = Date.now() / 1000
  const optimisticUserMessage: CommentMessage = {
    id: optimisticUserId,
    post_id: postId,
    soul_name: soulName,
    role: 'user',
    content,
    seq: nextSeq,
    created_at: createdAt,
    attachments,
  }
  const optimisticAssistantMessage: CommentMessage = {
    id: optimisticAssistantId,
    post_id: postId,
    soul_name: soulName,
    role: 'assistant',
    content: '',
    seq: nextSeq + 1,
    created_at: createdAt,
    attachments: [],
  }
  return {
    ...(current ?? { messages: [] }),
    messages: [...messages, optimisticUserMessage, optimisticAssistantMessage],
    sending: true,
    error: null,
  }
}

function withPendingCommentRerun(
  conversationsByPost: Record<string, Record<string, CommentConversationState>>,
  postId: string,
  commentId: number,
  rootComments: Comment[],
): Record<string, Record<string, CommentConversationState>> {
  const postConversations = conversationsByPost[postId] ?? {}
  const createdAt = Date.now() / 1000
  let foundMessage = false
  const nextPostConversations = Object.fromEntries(
    Object.entries(postConversations).map(([soulName, conversation]) => {
      const targetIndex = conversation.messages.findIndex((message) => message.id === commentId)
      if (targetIndex < 0) return [soulName, conversation]
      foundMessage = true
      return [soulName, withPendingCommentMessage(conversation, targetIndex, createdAt)]
    }),
  )

  const rootComment = rootComments.find((comment) => comment.id === commentId && comment.role === 'assistant')
  if (!foundMessage && rootComment) {
    const current = nextPostConversations[rootComment.soul_name] ?? { messages: [] }
    nextPostConversations[rootComment.soul_name] = {
      ...current,
      messages: [],
      sending: true,
      error: null,
    }
  }

  if (!foundMessage && !rootComment) return conversationsByPost

  return {
    ...conversationsByPost,
    [postId]: nextPostConversations,
  }
}

function withPendingCommentMessage(
  conversation: CommentConversationState,
  targetIndex: number,
  rerunAt: number,
): CommentConversationState {
  const targetMessage = conversation.messages[targetIndex]
  if (!targetMessage) return conversation
  return {
    ...conversation,
    messages: [
      ...conversation.messages.slice(0, targetIndex).map((message) => ({ ...message })),
      {
        ...targetMessage,
        content: '',
        metadata: null,
        rerun_at: rerunAt,
        attachments: [],
      },
    ],
    sending: true,
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

function latestEventId(events: PostEvent[]): number {
  return events.reduce((latest, event) => Math.max(latest, event.id), 0)
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function EmptyIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity="0.3">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
    </svg>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type Attachment,
  type Comment,
  type PostDetail,
  ApiError,
  deleteCommentMessage,
  getCommentConversation,
  getPost,
  listCommentConversations,
  rerunCommentMessage,
  retryJob,
  sendCommentMessage,
  streamPostEvents,
} from '@/api/client'
import { type CommentConversationState } from '@/components/PostCard'
import {
  buildSendingCommentState,
  failedCommentState,
  latestEventId,
  shouldRefreshPostDetail,
  toConversationState,
  withPendingCommentRerun,
} from '@/utils/commentState'

// Timeline still owns a mirror of this orchestration. Q-1/Q-2 can move feed onto
// this hook once the detail page has settled and the main feed path is less risky.
export interface UsePostDetailResult {
  post: PostDetail['post'] | null
  comments: Comment[]
  conversations: Record<string, CommentConversationState>
  loading: boolean
  notFound: boolean
  error: string | null
  busyCommentId: number | null
  retryingJobId: number | null
  reply(soulName: string, content: string, attachments: Attachment[]): Promise<void>
  deleteComment(commentId: number): Promise<void>
  rerunComment(commentId: number): Promise<void>
  retryJobs(jobIds: number[]): Promise<void>
  refresh(): Promise<PostDetail | null>
}

export function usePostDetail(postId: string): UsePostDetailResult {
  const [post, setPost] = useState<PostDetail['post'] | null>(null)
  const [comments, setComments] = useState<Comment[]>([])
  const [conversations, setConversations] = useState<Record<string, CommentConversationState>>({})
  const [loading, setLoading] = useState(true)
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busyCommentId, setBusyCommentId] = useState<number | null>(null)
  const [retryingJobId, setRetryingJobId] = useState<number | null>(null)
  const unsubscribeRef = useRef<(() => void) | null>(null)
  const retryPollTokenRef = useRef(0)

  const stopStream = useCallback(() => {
    unsubscribeRef.current?.()
    unsubscribeRef.current = null
  }, [])

  const refreshConversations = useCallback(async () => {
    const roots = await listCommentConversations(postId)
    const details = await Promise.all(
      roots.map(async (conversation) => {
        const detail = await getCommentConversation(postId, conversation.soul_name)
        return [conversation.soul_name, toConversationState(detail.conversation, detail.messages)] as const
      }),
    )
    setConversations((prev) => {
      const next: Record<string, CommentConversationState> = Object.fromEntries(details)
      /* Keep in-flight optimistic threads: a server snapshot taken mid-send
         would wipe the pending bubble and let it flash back later. */
      for (const [soulName, state] of Object.entries(prev)) {
        if (state.sending) next[soulName] = state
      }
      return next
    })
  }, [postId])

  const refresh = useCallback(async () => {
    try {
      const detail = await getPost(postId)
      setPost(detail.post)
      setComments(detail.comments)
      setNotFound(false)
      setError(null)
      await refreshConversations()
      return detail
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setNotFound(true)
        setPost(null)
        setComments([])
        setConversations({})
      } else {
        setError(err instanceof Error ? err.message : '加载失败')
      }
      return null
    }
  }, [postId, refreshConversations])

  const subscribeIfRunning = useCallback((detail: PostDetail) => {
    const state = detail.post.pipeline_status?.state
    if (state !== 'running' && state !== 'retrying') return
    stopStream()
    unsubscribeRef.current = streamPostEvents(
      postId,
      (event) => {
        setPost((current) => current ? { ...current, latest_event_type: event.event_type } : current)
        if (shouldRefreshPostDetail(event)) void refresh()
      },
      () => {
        setPost((current) => current ? { ...current, latest_event_type: 'pipeline_done' } : current)
        void refresh()
      },
      { afterEventId: latestEventId(detail.events) },
    )
  }, [postId, refresh, stopStream])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setPost(null)
    setComments([])
    setConversations({})
    setNotFound(false)
    setError(null)

    void (async () => {
      const detail = await refresh()
      if (cancelled) return
      if (detail) subscribeIfRunning(detail)
      setLoading(false)
    })()

    return () => {
      cancelled = true
      stopStream()
      retryPollTokenRef.current += 1
    }
  }, [postId, refresh, stopStream, subscribeIfRunning])

  const reply = useCallback(async (soulName: string, content: string, attachments: Attachment[]) => {
    const optimisticUserId = -Date.now()
    const optimisticAssistantId = optimisticUserId - 1
    setConversations((prev) => ({
      ...prev,
      [soulName]: buildSendingCommentState(
        prev[soulName],
        postId,
        soulName,
        content,
        attachments,
        optimisticUserId,
        optimisticAssistantId,
      ),
    }))

    try {
      const response = await sendCommentMessage(postId, soulName, content, attachments.map((attachment) => attachment.id))
      setConversations((prev) => ({
        ...prev,
        [soulName]: response.result.ok
          ? toConversationState(response.conversation, response.messages)
          : failedCommentState(response.conversation, response.messages, response.result.error),
      }))
      await refresh()
    } catch (err) {
      setConversations((prev) => ({
        ...prev,
        [soulName]: {
          ...(prev[soulName] ?? { messages: [] }),
          messages: (prev[soulName]?.messages ?? []).filter(
            (message) => message.id !== optimisticUserId && message.id !== optimisticAssistantId,
          ),
          sending: false,
          error: err instanceof Error ? err.message : '发送失败',
        },
      }))
      throw err
    }
  }, [postId, refresh])

  const deleteComment = useCallback(async (commentId: number) => {
    setBusyCommentId(commentId)
    try {
      await deleteCommentMessage(commentId)
      await refresh()
    } finally {
      setBusyCommentId(null)
    }
  }, [refresh])

  const rerunComment = useCallback(async (commentId: number) => {
    const previousConversations = conversations
    setBusyCommentId(commentId)
    setConversations((prev) => withPendingCommentRerun({ [postId]: prev }, postId, commentId, comments)[postId] ?? prev)
    try {
      const response = await rerunCommentMessage(commentId)
      setConversations((prev) => ({
        ...prev,
        [response.conversation.soul_name]: toConversationState(response.conversation, response.messages),
      }))
      await refresh()
    } catch (err) {
      setConversations(previousConversations)
      await refresh()
      throw err
    } finally {
      setBusyCommentId(null)
    }
  }, [comments, conversations, postId, refresh])

  const pollPostPipelineUntilSettled = useCallback(async () => {
    const token = retryPollTokenRef.current + 1
    retryPollTokenRef.current = token
    const deadline = Date.now() + 30_000
    while (Date.now() < deadline && retryPollTokenRef.current === token) {
      await sleep(3_000)
      if (retryPollTokenRef.current !== token) return
      const detail = await refresh()
      const state = detail?.post.pipeline_status?.state
      if (state === 'failed' || state === 'done' || state === 'idle') return
    }
    if (retryPollTokenRef.current === token) await refresh()
  }, [refresh])

  const retryJobs = useCallback(async (jobIds: number[]) => {
    const firstJobId = jobIds[0]
    if (firstJobId === undefined) return
    setRetryingJobId(firstJobId)
    setError(null)
    try {
      const beforeRetry = await getPost(postId)
      const afterEventId = latestEventId(beforeRetry.events)
      await Promise.all(jobIds.map((jobId) => retryJob(jobId)))
      await refresh()
      stopStream()
      unsubscribeRef.current = streamPostEvents(
        postId,
        (event) => {
          setPost((current) => current ? { ...current, latest_event_type: event.event_type } : current)
          if (shouldRefreshPostDetail(event)) void refresh()
        },
        () => {
          setPost((current) => current ? { ...current, latest_event_type: 'pipeline_done' } : current)
          void refresh()
        },
        { afterEventId },
      )
      void pollPostPipelineUntilSettled()
    } catch (err) {
      setError(err instanceof Error ? err.message : '重试失败')
    } finally {
      setRetryingJobId(null)
    }
  }, [pollPostPipelineUntilSettled, postId, refresh, stopStream])

  return {
    post,
    comments,
    conversations,
    loading,
    notFound,
    error,
    busyCommentId,
    retryingJobId,
    reply,
    deleteComment,
    rerunComment,
    retryJobs,
    refresh,
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

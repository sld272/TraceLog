import { memo, useCallback, useEffect, useRef, useState } from 'react'
import {
  type Attachment,
  type Comment,
  type Post,
  type PostDetail,
  type SearchMode,
  type SearchResultItem,
  type Suggestion,
  createPost,
  deleteCommentMessage,
  deletePost,
  getPost,
  listPendingSuggestions,
  listPosts,
  postIdFromEvidenceRef,
  restartJob,
  retryJob,
  searchPosts,
  sendCommentMessage,
  streamPostEvents,
  rerunCommentMessage,
} from '@/api/client'
import { Composer } from '@/components/Composer'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { Notice } from '@/components/Notice'
import { type CommentConversationState, PostCard } from '@/components/PostCard'
import { formatRoute } from '@/router'
import { type PostMutationSignal } from '@/types/postMutation'
import {
  buildSendingCommentState,
  commentCountOf,
  conversationsFromThreads,
  failedCommentState,
  latestEventId,
  shouldRefreshPostDetail,
  toConversationState,
  withPendingCommentRerun,
} from '@/utils/commentState'
import { API_LIMITS } from '@/utils/constants'
import { localDateKey, monthDayLabel, weekdayLabel } from '@/utils/schedule'
import { dayAnchorLabel, dayKeyOf } from '@/utils/date'
import styles from './Timeline.module.css'

interface TimelineProps {
  onActivitySettled?: () => void
  modelConfigured?: boolean | null
  onOpenSettings?: () => void
  postMutationSignal?: PostMutationSignal | null
  /** Search is driven by the right panel input; query is lifted to App. */
  searchQuery: string
  /** 日期透镜选中的日期（null = 最新流）。 */
  selectedDate?: string | null
  /** 退出日期透镜（×、Esc、点收起条时调用）。 */
  onExitDateLens?: () => void
}

export function Timeline({
  onActivitySettled,
  modelConfigured,
  onOpenSettings,
  postMutationSignal,
  searchQuery,
  selectedDate = null,
  onExitDateLens,
}: TimelineProps) {
  const [posts, setPosts] = useState<Post[]>([])
  const [postComments, setPostComments] = useState<Record<string, Comment[]>>({})
  const [postSuggestions, setPostSuggestions] = useState<Record<string, Suggestion[]>>({})
  const [postCommentConversations, setPostCommentConversations] = useState<Record<string, Record<string, CommentConversationState>>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [deletingPostId, setDeletingPostId] = useState<string | null>(null)
  const [busyCommentId, setBusyCommentId] = useState<number | null>(null)
  const [retryingJobId, setRetryingJobId] = useState<number | null>(null)
  const [expandingPostIds, setExpandingPostIds] = useState<Record<string, boolean>>({})
  const [expandErrors, setExpandErrors] = useState<Record<string, boolean>>({})
  const [searchResults, setSearchResults] = useState<SearchResultItem[]>([])
  const [searchMode, setSearchMode] = useState<SearchMode>('keyword')
  const [semanticAvailable, setSemanticAvailable] = useState<boolean | null>(null)
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [hasMorePosts, setHasMorePosts] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean
    title: string
    message: string
    onConfirm: () => void
  } | null>(null)
  const postStreamUnsubscribersRef = useRef<Map<string, () => void>>(new Map())
  const loadMoreSentinelRef = useRef<HTMLDivElement>(null)
  const retryPollTokenRef = useRef(0)
  const searchTokenRef = useRef(0)
  const searchTimerRef = useRef<number | null>(null)
  const lastHybridQueryRef = useRef<string | null>(null)
  const modelUnavailable = modelConfigured === false
  const trimmedSearchQuery = searchQuery.trim()
  const searching = trimmedSearchQuery.length > 0
  const dateLensActive = selectedDate !== null && !searching
  const filteredPosts = dateLensActive
    ? posts.filter((post) => localDateKey(post.ts) === selectedDate)
    : posts

  /* Esc 退出日期透镜（搜索态下不劫持 Esc，交给搜索框）。 */
  useEffect(() => {
    if (!dateLensActive) return
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onExitDateLens?.()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [dateLensActive, onExitDateLens])

  /* Pending suggestions belong to the post (not its comments), so they are
     fetched independently and keyed by post id — this keeps the prompt under
     the post visible regardless of whether comments are loaded/expanded. */
  const refreshSuggestions = useCallback(async () => {
    try {
      const all = await listPendingSuggestions()
      const grouped: Record<string, Suggestion[]> = {}
      for (const suggestion of all) {
        const postId = postIdFromEvidenceRef(suggestion.evidence_ref)
        if (!postId) continue
        ;(grouped[postId] ??= []).push(suggestion)
      }
      setPostSuggestions(grouped)
    } catch {
      /* keep the previous suggestions on a transient failure */
    }
  }, [])

  /* 列表接口已经带回每位好友的首条回应和最新一个来回，直接铺进状态即可。
     正在发送的那条原样留着：服务端快照是发送前拍的，盖上去会让刚打出去的
     追问先消失、等回复到了再闪回来。 */
  const adoptListedComments = useCallback((list: Post[]) => {
    setPostComments((prev) => {
      const next = { ...prev }
      for (const post of list) next[post.post_id] = post.comments
      return next
    })
    setPostCommentConversations((prev) => {
      const next = { ...prev }
      for (const post of list) {
        next[post.post_id] = conversationsFromThreads(post.conversations, prev[post.post_id])
      }
      return next
    })
  }, [])

  const fetchPosts = useCallback(async () => {
    try {
      const data = await listPosts(API_LIMITS.POSTS_DEFAULT, 0)
      setPosts(data)
      setHasMorePosts(data.length >= API_LIMITS.POSTS_DEFAULT)
      setError(null)
      void refreshSuggestions()
      adoptListedComments(data)
      data.forEach((post) => {
        if (isActivePipeline(post)) void restorePostStream(post.post_id)
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [refreshSuggestions, adoptListedComments])

  useEffect(() => {
    fetchPosts()
  }, [fetchPosts])

  const runSearch = useCallback(async (query: string, mode: SearchMode = 'keyword') => {
    const clean = query.trim()
    const token = searchTokenRef.current + 1
    searchTokenRef.current = token
    if (!clean) {
      setSearchResults([])
      setSearchError(null)
      setSearchMode('keyword')
      setSemanticAvailable(null)
      setSearchLoading(false)
      return
    }
    setSearchMode(mode)
    setSearchLoading(true)
    setSearchError(null)
    try {
      const response = await searchPosts(clean, 20, mode)
      if (searchTokenRef.current !== token) return
      const items = Array.isArray(response.items) ? response.items : []
      setSearchResults(items)
      adoptListedComments(items)
      setSearchMode(response.mode ?? mode)
      setSemanticAvailable(response.semantic_available ?? null)
    } catch (err) {
      if (searchTokenRef.current !== token) return
      if (mode === 'hybrid') lastHybridQueryRef.current = null
      setSearchError(err instanceof Error ? err.message : '搜索失败')
    } finally {
      if (searchTokenRef.current === token) setSearchLoading(false)
    }
  }, [adoptListedComments])

  const clearSearchTimer = useCallback(() => {
    if (searchTimerRef.current !== null) {
      window.clearTimeout(searchTimerRef.current)
      searchTimerRef.current = null
    }
  }, [])

  /* 按去掉首尾空格后的词去防抖：多敲一个空格不该让整片结果重跑一遍。 */
  useEffect(() => {
    lastHybridQueryRef.current = null
    searchTimerRef.current = window.setTimeout(() => {
      searchTimerRef.current = null
      void runSearch(trimmedSearchQuery, 'keyword')
    }, 300)
    return clearSearchTimer
  }, [clearSearchTimer, runSearch, trimmedSearchQuery])

  useEffect(() => {
    return () => {
      stopAllPostStreams()
      retryPollTokenRef.current += 1
      searchTokenRef.current += 1
    }
  }, [])

  const runDeepSearch = () => {
    const clean = searchQuery.trim()
    if (!clean) return
    if (lastHybridQueryRef.current === clean && searchMode === 'hybrid') return
    clearSearchTimer()
    lastHybridQueryRef.current = clean
    void runSearch(clean, 'hybrid')
  }

  const handleSubmit = async (content: string, attachments: Attachment[]) => {
    if (modelUnavailable) {
      const message = '请先在设置中配置主模型和 Embedding，再发布记录。'
      setError(message)
      throw new Error(message)
    }
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
      goal_activities: [],
      comments: [],
      conversations: [],
    }
    setPosts((prev) => [newPost, ...prev])

    subscribeToPost(result.post_id)
  }

  const stopPostStream = (postId: string) => {
    postStreamUnsubscribersRef.current.get(postId)?.()
    postStreamUnsubscribersRef.current.delete(postId)
  }

  const stopAllPostStreams = () => {
    postStreamUnsubscribersRef.current.forEach((unsubscribe) => unsubscribe())
    postStreamUnsubscribersRef.current.clear()
  }

  const applyPostDetailToSummary = (detail: PostDetail, eventType?: string) => {
    setPosts((prev) =>
      prev.map((p) =>
        p.post_id === detail.post.post_id
          ? {
              ...p,
              importance: detail.post.importance,
              comment_count: commentCountOf(detail.comments, detail.conversations),
              latest_event_type: detail.post.latest_event_type ?? eventType ?? p.latest_event_type,
              pipeline_status: detail.post.pipeline_status,
              attachments: detail.post.attachments,
              goal_activities: detail.post.goal_activities,
            }
          : p,
      ),
    )
  }

  const restorePostStream = async (postId: string) => {
    try {
      const detail = await getPost(postId)
      applyPostDetailToSummary(detail)
      if (!isActivePipeline(detail.post)) {
        stopPostStream(postId)
        return
      }
      subscribeToPost(postId, latestEventId(detail.events))
    } catch {
      /* Keep the row visible; the next list refresh can try restoring again. */
    }
  }

  const subscribeToPost = (postId: string, afterEventId?: number) => {
    stopPostStream(postId)
    const unsubscribe = streamPostEvents(
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
          void refreshPostDetail(postId, event.event_type)
          void refreshSuggestions()
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
        stopPostStream(postId)
        void refreshPostDetail(postId)
        void refreshSuggestions()
        onActivitySettled?.()
      },
      afterEventId === undefined ? {} : { afterEventId },
    )
    postStreamUnsubscribersRef.current.set(postId, unsubscribe)
  }

  const loadMorePosts = useCallback(async () => {
    if (loadingMore || !hasMorePosts || searching || posts.length === 0) return
    const cursorPost = posts[posts.length - 1]
    if (!cursorPost) return
    setLoadingMore(true)
    try {
      const data = await listPosts(
        API_LIMITS.POSTS_DEFAULT,
        0,
        { beforeTs: cursorPost.ts, beforeId: cursorPost.post_id },
      )
      setPosts((prev) => appendUniquePosts(prev, data))
      setHasMorePosts(data.length >= API_LIMITS.POSTS_DEFAULT)
      setError(null)
      adoptListedComments(data)
      data.forEach((post) => {
        if (isActivePipeline(post)) void restorePostStream(post.post_id)
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载更早记录失败')
    } finally {
      setLoadingMore(false)
    }
  }, [hasMorePosts, loadingMore, posts, searching, adoptListedComments])

  useEffect(() => {
    if (searching || !hasMorePosts || loadingMore) return
    const sentinel = loadMoreSentinelRef.current
    if (!sentinel) return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void loadMorePosts()
    }, { threshold: 0.01 })
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [hasMorePosts, loadMorePosts, loadingMore, searching, posts.length])

  const refreshPostDetail = async (postId: string, eventType?: string) => {
    try {
      const detail = await getPost(postId)
      setPostComments((prev) => ({
        ...prev,
        [postId]: detail.comments,
      }))
      setPostCommentConversations((prev) => ({
        ...prev,
        [postId]: conversationsFromThreads(detail.conversations, prev[postId]),
      }))
      applyPostDetailToSummary(detail, eventType)
      return detail
    } catch {
      /* keep the optimistic post visible if detail refresh fails */
      return null
    }
  }

  /* Refresh list-row fields only, without expanding the post's comments. */
  const refreshPostSummary = async (postId: string) => {
    try {
      const detail = await getPost(postId)
      applyPostDetailToSummary(detail)
    } catch {
      /* keep the stale summary if refresh fails */
    }
  }

  useEffect(() => {
    if (!postMutationSignal) return
    const { postId, kind } = postMutationSignal
    if (kind === 'deleted') {
      stopPostStream(postId)
      setPosts((prev) => prev.filter((post) => post.post_id !== postId))
      setSearchResults((prev) => prev.filter((post) => post.post_id !== postId))
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
      return
    }
    /* Only posts the user already expanded get their comments refreshed;
       collapsed posts stay collapsed and just update their summary row. */
    if (postComments[postId]) {
      void refreshPostDetail(postId)
    } else {
      void refreshPostSummary(postId)
    }
  }, [postMutationSignal])

  const handleExpand = async (postId: string) => {
    setExpandingPostIds((prev) => ({ ...prev, [postId]: true }))
    setExpandErrors((prev) => ({ ...prev, [postId]: false }))
    try {
      const detail = await getPost(postId)
      setPostComments((prev) => ({ ...prev, [postId]: detail.comments }))
      setPostCommentConversations((prev) => ({
        ...prev,
        [postId]: conversationsFromThreads(detail.conversations, prev[postId]),
      }))
    } catch {
      setExpandErrors((prev) => ({
        ...prev,
        [postId]: true,
      }))
    } finally {
      setExpandingPostIds((prev) => ({ ...prev, [postId]: false }))
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
      title: '删除记录',
      message: '删除这条记录会同时删除 TA 们的所有回应和追问，且不会自动恢复。确定删除吗？',
      onConfirm: async () => {
        setConfirmDialog(null)
        setDeletingPostId(postId)
        try {
          await deletePost(postId)
          stopPostStream(postId)
          setPosts((prev) => prev.filter((post) => post.post_id !== postId))
          setSearchResults((prev) => prev.filter((post) => post.post_id !== postId))
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
      title: '删除追问',
      message: '删除这条追问会同时删除它之后的这段对话，且不会自动恢复。确定删除吗？',
      onConfirm: async () => {
        setConfirmDialog(null)
        setBusyCommentId(commentId)
        try {
          await deleteCommentMessage(commentId)
          await refreshPostDetail(postId)
        } catch (err) {
          setError(err instanceof Error ? err.message : '删除追问失败')
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
    } catch {
      setPostCommentConversations(previousConversations)
      await refreshPostDetail(postId)
      setError('本次重跑未完成，请稍后再试。')
    } finally {
      setBusyCommentId(null)
    }
  }

  const runPostJobsAgain = async (
    postId: string,
    jobIds: number[],
    submit: (jobId: number) => Promise<unknown>,
  ) => {
    const firstJobId = jobIds[0]
    if (firstJobId === undefined) return
    setRetryingJobId(firstJobId)
    setError(null)
    try {
      const beforeRetry = await getPost(postId)
      const afterEventId = latestEventId(beforeRetry.events)
      await Promise.all(jobIds.map((jobId) => submit(jobId)))
      await refreshPostDetail(postId)
      subscribeToPost(postId, afterEventId)
      pollPostPipelineUntilSettled(postId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '重试失败')
    } finally {
      setRetryingJobId(null)
    }
  }

  const handleRetryPostJobs = (postId: string, jobIds: number[]) =>
    runPostJobsAgain(postId, jobIds, retryJob)

  /* 等太久时用户自己按下的重试：放弃还没有结果的那几个，重排一份新的。 */
  const handleRestartPostJobs = (postId: string, jobIds: number[]) =>
    runPostJobsAgain(postId, jobIds, restartJob)

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

  const renderPostCard = (post: Post) => (
    <TimelinePostCard
      key={post.post_id}
      post={post}
      comments={postComments[post.post_id]}
      suggestions={postSuggestions[post.post_id]}
      commentConversations={postCommentConversations[post.post_id]}
      busyCommentId={busyCommentId}
      deletingPost={deletingPostId === post.post_id}
      retryingJobId={retryingJobId}
      modelConfigured={modelConfigured}
      expandLoading={expandingPostIds[post.post_id] ?? false}
      expandError={expandErrors[post.post_id] ?? false}
      onExpandPost={handleExpand}
      onReplyPost={handleCommentReply}
      onDeletePostById={handleDeletePost}
      onDeleteCommentById={handleDeleteComment}
      onRerunCommentById={handleRerunComment}
      onRetryPostJobs={handleRetryPostJobs}
      onRestartPostJobs={handleRestartPostJobs}
    />
  )

  const renderDayGroups = (list: Post[]) =>
    groupPostsByDay(list).map((group) => (
      <section key={group.key} className={styles.dayGroup}>
        <DayAnchor dayKey={group.key} />
        <div className={styles.dayPosts}>{group.posts.map(renderPostCard)}</div>
      </section>
    ))

  return (
    <div className={styles.timeline}>
      <TimelineHeader />

      {modelUnavailable && onOpenSettings && (
        <section className={styles.firstRunGuide}>
          <div>
            <h2>欢迎来到 TraceLog</h2>
            <p>先去设置里填好 API Key，就可以开始记录，让拾迹陪你回看一路走来的变化。</p>
            <p className={styles.firstRunPrivacy}>调试日志默认不记录对话内容，你随时可以在设置里调整。</p>
          </div>
          <button type="button" onClick={onOpenSettings}>去设置</button>
        </section>
      )}

      {dateLensActive && selectedDate && (
        <div className={styles.filterBar} role="status">
          <span className={styles.filterBarIcon}><FilterCalendarIcon /></span>
          <span className={styles.filterBarText}>
            <strong>{monthDayLabel(selectedDate)} {weekdayLabel(selectedDate)}</strong>
            <span className={styles.filterCount}>
              {filteredPosts.length > 0 ? `${filteredPosts.length} 条帖子` : '没有帖子'}
            </span>
          </span>
          <span className={styles.filterBarHint}>Esc 退出</span>
          <button className={styles.filterExit} type="button" onClick={onExitDateLens}>× 回到最新</button>
        </div>
      )}

      {dateLensActive ? (
        <button className={styles.composerCollapsed} type="button" onClick={onExitDateLens}>
          <BackArrowIcon />
          正在回看过去的一天 — 回到最新，发布此刻的动态
        </button>
      ) : (
        <Composer
          onSubmit={handleSubmit}
          disabled={modelUnavailable}
          disabledReason="主模型和 Embedding 尚未配置，配置完成后才能发布记录。"
        />
      )}

      {error && posts.length === 0 ? (
        <div className={styles.error}>
          <p>无法加载时间线</p>
          <p className={styles.errorDetail}>{error}</p>
          <button className={styles.retryBtn} onClick={fetchPosts}>重试</button>
        </div>
      ) : (
        <>
          {error && (
            <Notice kind="error" onClose={() => setError(null)}>
              {error}
            </Notice>
          )}
          {searching ? (
            <SearchResults
              query={trimmedSearchQuery}
              results={searchResults}
              mode={searchMode}
              semanticAvailable={semanticAvailable}
              loading={searchLoading}
              error={searchError}
              onDeepSearch={runDeepSearch}
              onRetry={() => runSearch(searchQuery, searchMode)}
            />
          ) : dateLensActive ? (
            filteredPosts.length === 0 ? (
              <div className={styles.feedEmpty}>
                <strong>{selectedDate && monthDayLabel(selectedDate)}没有帖子</strong>
                右边可以看看这天的日程，或者回到最新动态。
              </div>
            ) : (
              <div className={styles.feed}>{renderDayGroups(filteredPosts)}</div>
            )
          ) : posts.length === 0 ? (
            <div className={styles.empty}>
              <EmptyIcon />
              <p className={styles.emptyTitle}>还没有记录</p>
              <p className={styles.emptyHint}>
                {modelUnavailable
                  ? '完成上方设置后，写下你的第一条想法。'
                  : '写下你的第一条想法，TA 们会回应你'}
              </p>
            </div>
          ) : (
            <div className={styles.feed}>
              {renderDayGroups(posts)}
              <div className={styles.loadMoreRow} ref={loadMoreSentinelRef}>
                {loadingMore ? (
                  <span>加载更早的记录...</span>
                ) : hasMorePosts ? (
                  <button type="button" onClick={loadMorePosts}>
                    加载更早的记录
                  </button>
                ) : (
                  <span>已经是最早的记录</span>
                )}
              </div>
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

function SearchResults({
  query,
  results,
  mode,
  semanticAvailable,
  loading,
  error,
  onDeepSearch,
  onRetry,
}: {
  query: string
  results: SearchResultItem[]
  mode: SearchMode
  semanticAvailable: boolean | null
  loading: boolean
  error: string | null
  onDeepSearch: () => void
  onRetry: () => void
}) {
  const deepDisabled = semanticAvailable === false
  const summary = searchSummaryText(results.length, mode, loading, semanticAvailable)

  return (
    <div className={styles.searchResults}>
      <div className={styles.searchSummary}>
        {error ? (
          <>
            <span>搜索失败：{error}</span>
            <button onClick={onRetry}>重试</button>
          </>
        ) : (
          <>
            <span>{summary}</span>
            {mode === 'keyword' && (
              <button
                className={styles.searchDeepButton}
                onClick={onDeepSearch}
                disabled={deepDisabled}
                title={deepDisabled ? '需要先在设置中配置 Embedding' : '使用语义检索扩展搜索结果'}
              >
                深度搜索
              </button>
            )}
          </>
        )}
      </div>
      {mode === 'hybrid' && semanticAvailable === false && !error && (
        <p className={styles.searchHint}>语义检索暂不可用，以下为关键词结果</p>
      )}
      {!loading && !error && results.length === 0 && (
        <div className={styles.empty}>
          <p className={styles.emptyTitle}>没有找到与「{query}」相关的记录</p>
        </div>
      )}
      {results.length > 0 && (
        <div className={styles.feed}>
          {results.map((post) => {
            const href = formatRoute({ kind: 'post', postId: post.post_id })
            return (
              <div
                key={post.post_id}
                className={styles.searchResultCard}
                role="link"
                tabIndex={0}
                onClick={() => { window.location.hash = href }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    window.location.hash = href
                  }
                }}
              >
                {post.match === 'semantic' && (
                  <span className={styles.semanticBadge}>语义相关</span>
                )}
                <PostCard
                  post={post}
                  detailHref={href}
                />
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

const TimelinePostCard = memo(function TimelinePostCard({
  post,
  comments,
  suggestions,
  commentConversations,
  busyCommentId,
  deletingPost,
  retryingJobId,
  modelConfigured,
  expandLoading,
  expandError,
  onExpandPost,
  onReplyPost,
  onDeletePostById,
  onDeleteCommentById,
  onRerunCommentById,
  onRetryPostJobs,
  onRestartPostJobs,
}: {
  post: Post
  comments?: Comment[]
  suggestions?: Suggestion[]
  commentConversations?: Record<string, CommentConversationState>
  busyCommentId: number | null
  deletingPost: boolean
  retryingJobId: number | null
  modelConfigured?: boolean | null
  expandLoading: boolean
  expandError: boolean
  onExpandPost: (postId: string) => Promise<void>
  onReplyPost: (postId: string, soulName: string, content: string, attachments: Attachment[]) => Promise<void>
  onDeletePostById: (postId: string) => Promise<void>
  onDeleteCommentById: (postId: string, commentId: number) => Promise<void>
  onRerunCommentById: (postId: string, commentId: number) => Promise<void>
  onRetryPostJobs: (postId: string, jobIds: number[]) => Promise<void>
  onRestartPostJobs: (postId: string, jobIds: number[]) => Promise<void>
}) {
  const detailHref = formatRoute({ kind: 'post', postId: post.post_id })
  const handleExpand = useCallback(() => onExpandPost(post.post_id), [onExpandPost, post.post_id])
  const handleReply = useCallback(
    (soulName: string, content: string, attachments: Attachment[]) =>
      onReplyPost(post.post_id, soulName, content, attachments),
    [onReplyPost, post.post_id],
  )
  const handleDeletePost = useCallback(() => onDeletePostById(post.post_id), [onDeletePostById, post.post_id])
  const handleDeleteComment = useCallback(
    (commentId: number) => onDeleteCommentById(post.post_id, commentId),
    [onDeleteCommentById, post.post_id],
  )
  const handleRerunComment = useCallback(
    (commentId: number) => onRerunCommentById(post.post_id, commentId),
    [onRerunCommentById, post.post_id],
  )
  const handleRetryJobs = useCallback(
    (jobIds: number[]) => onRetryPostJobs(post.post_id, jobIds),
    [onRetryPostJobs, post.post_id],
  )
  const handleRestartJobs = useCallback(
    (jobIds: number[]) => onRestartPostJobs(post.post_id, jobIds),
    [onRestartPostJobs, post.post_id],
  )

  return (
    <ErrorBoundary
      variant="inline"
      title="此条内容无法显示"
      message="其他记录不受影响，可以刷新页面后再试。"
    >
      <PostCard
        post={post}
        comments={comments}
        suggestions={suggestions}
        commentConversations={commentConversations}
        busyCommentId={busyCommentId}
        deletingPost={deletingPost}
        retryingJobId={retryingJobId}
        detailHref={detailHref}
        timeStyle="clock"
        modelConfigured={modelConfigured}
        expandLoading={expandLoading}
        expandError={expandError}
        onExpand={handleExpand}
        onReply={handleReply}
        onDeletePost={handleDeletePost}
        onDeleteComment={handleDeleteComment}
        onRerunComment={handleRerunComment}
        onRetryFailedJobs={handleRetryJobs}
        onRestartStuckJobs={handleRestartJobs}
      />
    </ErrorBoundary>
  )
})

function searchSummaryText(
  count: number,
  mode: SearchMode,
  loading: boolean,
  semanticAvailable: boolean | null,
): string {
  if (loading && mode === 'hybrid') return '正在语义检索...'
  if (loading) return '正在搜索...'
  if (mode === 'hybrid' && semanticAvailable === false) return `找到 ${count} 条记录`
  if (mode === 'hybrid') return `共 ${count} 条 · 已深度搜索`
  return `找到 ${count} 条记录`
}

/* 首页开头只说一句话。日期由下面每一组的锚点交代，不在这里重复；
   产品是干什么的也不必在自己的首页上介绍一遍。 */
function TimelineHeader() {
  const hour = new Date().getHours()
  const greeting =
    hour < 5 ? '夜深了' : hour < 11 ? '早上好' : hour < 13 ? '中午好' : hour < 18 ? '下午好' : '晚上好'
  return (
    <header className={styles.header}>
      <h1>{greeting}</h1>
    </header>
  )
}

/** 时间线的日期锚：日号挂在左边，帖子挂在它下面。 */
function DayAnchor({ dayKey }: { dayKey: string }) {
  const label = dayAnchorLabel(dayKey)
  return (
    <div className={styles.dayAnchor}>
      <span className={styles.dayNumber} data-numeric>{label.day}</span>
      <span className={styles.dayDetail}>
        {label.relative && <strong>{label.relative}</strong>}
        {label.detail}
      </span>
    </div>
  )
}

/** 把时间线按自然日切段，日期只在段首出现一次。 */
function groupPostsByDay(posts: Post[]): { key: string; posts: Post[] }[] {
  const groups: { key: string; posts: Post[] }[] = []
  for (const post of posts) {
    const key = dayKeyOf(post.ts)
    const last = groups[groups.length - 1]
    if (last && last.key === key) last.posts.push(post)
    else groups.push({ key, posts: [post] })
  }
  return groups
}

function FilterCalendarIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="18" height="18" rx="2" />
      <line x1="16" y1="2" x2="16" y2="6" />
      <line x1="8" y1="2" x2="8" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
    </svg>
  )
}

function BackArrowIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 12h18M3 12l6-6M3 12l6 6" />
    </svg>
  )
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function isActivePipeline(post: Pick<Post, 'pipeline_status'>): boolean {
  const state = post.pipeline_status?.state
  return state === 'running' || state === 'retrying'
}

function appendUniquePosts(current: Post[], incoming: Post[]): Post[] {
  if (incoming.length === 0) return current
  const seen = new Set(current.map((post) => post.post_id))
  const next = [...current]
  for (const post of incoming) {
    if (seen.has(post.post_id)) continue
    seen.add(post.post_id)
    next.push(post)
  }
  return next
}

function EmptyIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity="0.3">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
    </svg>
  )
}

import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import {
  type Attachment,
  type Comment,
  type Post,
  type PostDetail,
  type SearchMode,
  type SearchResultItem,
  createPost,
  deleteCommentMessage,
  deletePost,
  getCommentConversation,
  getPost,
  listCommentConversations,
  listPosts,
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
  failedCommentState,
  latestEventId,
  shouldRefreshPostDetail,
  toConversationState,
  withPendingCommentRerun,
} from '@/utils/commentState'
import { API_LIMITS } from '@/utils/constants'
import styles from './Timeline.module.css'

interface TimelineProps {
  onActivitySettled?: () => void
  onTodosChanged?: () => void
  modelConfigured?: boolean | null
  onOpenSettings?: () => void
  postMutationSignal?: PostMutationSignal | null
}

export function Timeline({
  onActivitySettled,
  onTodosChanged,
  modelConfigured,
  onOpenSettings,
  postMutationSignal,
}: TimelineProps) {
  const [posts, setPosts] = useState<Post[]>([])
  const [postComments, setPostComments] = useState<Record<string, Comment[]>>({})
  const [postCommentConversations, setPostCommentConversations] = useState<Record<string, Record<string, CommentConversationState>>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [deletingPostId, setDeletingPostId] = useState<string | null>(null)
  const [busyCommentId, setBusyCommentId] = useState<number | null>(null)
  const [retryingJobId, setRetryingJobId] = useState<number | null>(null)
  const [expandingPostIds, setExpandingPostIds] = useState<Record<string, boolean>>({})
  const [expandErrors, setExpandErrors] = useState<Record<string, string | null>>({})
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<SearchResultItem[]>([])
  const [searchMode, setSearchMode] = useState<SearchMode>('keyword')
  const [semanticAvailable, setSemanticAvailable] = useState<boolean | null>(null)
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean
    title: string
    message: string
    onConfirm: () => void
  } | null>(null)
  const postStreamUnsubscribersRef = useRef<Map<string, () => void>>(new Map())
  const retryPollTokenRef = useRef(0)
  const searchTokenRef = useRef(0)
  const searchTimerRef = useRef<number | null>(null)
  const lastHybridQueryRef = useRef<string | null>(null)
  const modelUnavailable = modelConfigured === false
  const trimmedSearchQuery = searchQuery.trim()
  const searching = trimmedSearchQuery.length > 0

  const fetchPosts = useCallback(async () => {
    try {
      const data = await listPosts(API_LIMITS.POSTS_DEFAULT, 0)
      setPosts(data)
      setError(null)
      data.forEach((post) => {
        if (isActivePipeline(post)) void restorePostStream(post.post_id)
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

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
      setSearchResults(Array.isArray(response.items) ? response.items : [])
      setSearchMode(response.mode ?? mode)
      setSemanticAvailable(response.semantic_available ?? null)
    } catch (err) {
      if (searchTokenRef.current !== token) return
      if (mode === 'hybrid') lastHybridQueryRef.current = null
      setSearchError(err instanceof Error ? err.message : '搜索失败')
    } finally {
      if (searchTokenRef.current === token) setSearchLoading(false)
    }
  }, [])

  const clearSearchTimer = useCallback(() => {
    if (searchTimerRef.current !== null) {
      window.clearTimeout(searchTimerRef.current)
      searchTimerRef.current = null
    }
  }, [])

  useEffect(() => {
    searchTimerRef.current = window.setTimeout(() => {
      searchTimerRef.current = null
      void runSearch(searchQuery, 'keyword')
    }, 300)
    return clearSearchTimer
  }, [clearSearchTimer, runSearch, searchQuery])

  useEffect(() => {
    return () => {
      stopAllPostStreams()
      retryPollTokenRef.current += 1
      searchTokenRef.current += 1
    }
  }, [])

  const clearSearch = () => {
    searchTokenRef.current += 1
    lastHybridQueryRef.current = null
    setSearchQuery('')
    setSearchResults([])
    setSearchMode('keyword')
    setSemanticAvailable(null)
    setSearchError(null)
    setSearchLoading(false)
  }

  const handleSearchChange = (value: string) => {
    setSearchQuery(value)
    setSearchMode('keyword')
    lastHybridQueryRef.current = null
  }

  const runDeepSearch = () => {
    const clean = searchQuery.trim()
    if (!clean) return
    if (lastHybridQueryRef.current === clean && searchMode === 'hybrid') return
    clearSearchTimer()
    lastHybridQueryRef.current = clean
    void runSearch(clean, 'hybrid')
  }

  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return
    if (event.key === 'Enter') {
      event.preventDefault()
      runDeepSearch()
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      clearSearch()
    }
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
              comment_count: detail.comments.length,
              latest_event_type: detail.post.latest_event_type ?? eventType ?? p.latest_event_type,
              pipeline_status: detail.post.pipeline_status,
              attachments: detail.post.attachments,
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
        stopPostStream(postId)
        void refreshPostDetail(postId)
        onActivitySettled?.()
      },
      afterEventId === undefined ? {} : { afterEventId },
    )
    postStreamUnsubscribersRef.current.set(postId, unsubscribe)
  }

  const refreshPostDetail = async (postId: string, eventType?: string) => {
    try {
      const detail = await getPost(postId)
      setPostComments((prev) => ({
        ...prev,
        [postId]: detail.comments,
      }))
      await refreshCommentConversations(postId)
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
    setExpandErrors((prev) => ({ ...prev, [postId]: null }))
    try {
      const detail = await getPost(postId)
      setPostComments((prev) => ({ ...prev, [postId]: detail.comments }))
      await refreshCommentConversations(postId)
    } catch (err) {
      setExpandErrors((prev) => ({
        ...prev,
        [postId]: err instanceof Error ? err.message : '加载失败',
      }))
    } finally {
      setExpandingPostIds((prev) => ({ ...prev, [postId]: false }))
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
      setPostCommentConversations((prev) => {
        const next: Record<string, CommentConversationState> = Object.fromEntries(details)
        /* Keep in-flight optimistic threads: a server snapshot taken mid-send
           would wipe the pending bubble and let it flash back later. */
        for (const [soulName, state] of Object.entries(prev[postId] ?? {})) {
          if (state.sending) next[soulName] = state
        }
        return { ...prev, [postId]: next }
      })
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
      subscribeToPost(postId, afterEventId)
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
      <div className={styles.searchBox}>
        <SearchIcon />
        <input
          value={searchQuery}
          onChange={(event) => handleSearchChange(event.target.value)}
          onKeyDown={handleSearchKeyDown}
          placeholder="搜索记录"
          aria-label="搜索记录"
        />
        {searchLoading && <span className={styles.searchLoading}>搜索中...</span>}
        {searching && (
          <button className={styles.searchClear} onClick={clearSearch} aria-label="清空搜索" title="清空搜索">
            ×
          </button>
        )}
      </div>
      <Composer
        onSubmit={handleSubmit}
        disabled={modelUnavailable}
        disabledReason="主模型和 Embedding 尚未配置，配置完成后才能发布记录。"
      />

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
          ) : posts.length === 0 ? (
            <div className={styles.empty}>
              <EmptyIcon />
              <p className={styles.emptyTitle}>{modelUnavailable ? '先配置模型' : '还没有记录'}</p>
              <p className={styles.emptyHint}>
                {modelUnavailable
                  ? '配置主模型和 Embedding 后，就可以开始记录并生成回应。'
                  : '写下你的第一条想法，TA 们会回应你'}
              </p>
              {modelUnavailable && onOpenSettings && (
                <button className={styles.emptyAction} onClick={onOpenSettings}>
                  去设置
                </button>
              )}
            </div>
          ) : (
            <div className={styles.feed}>
              {posts.map((post) => (
                <ErrorBoundary
                  key={post.post_id}
                  variant="inline"
                  title="此条内容无法显示"
                  message="其他记录不受影响，可以刷新页面后再试。"
                >
                  <PostCard
                    post={post}
                    comments={postComments[post.post_id]}
                    commentConversations={postCommentConversations[post.post_id]}
                    busyCommentId={busyCommentId}
                    deletingPost={deletingPostId === post.post_id}
                    retryingJobId={retryingJobId}
                    detailHref={formatRoute({ kind: 'post', postId: post.post_id })}
                    modelConfigured={modelConfigured}
                    expandLoading={expandingPostIds[post.post_id] ?? false}
                    expandError={expandErrors[post.post_id] ?? null}
                    onExpand={() => handleExpand(post.post_id)}
                    onReply={(soulName, content, attachments) => handleCommentReply(post.post_id, soulName, content, attachments)}
                    onDeletePost={() => handleDeletePost(post.post_id)}
                    onDeleteComment={(commentId) => handleDeleteComment(post.post_id, commentId)}
                    onRerunComment={(commentId) => handleRerunComment(post.post_id, commentId)}
                    onRetryFailedJobs={(jobIds) => handleRetryPostJobs(post.post_id, jobIds)}
                  />
                </ErrorBoundary>
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

function TimelineHeader() {
  return (
    <header className={styles.header}>
      <div>
        <h1>首页</h1>
        <p>记录、回应、整理都流回这里</p>
      </div>
    </header>
  )
}

function SearchIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.35-4.35" />
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

function EmptyIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity="0.3">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
    </svg>
  )
}

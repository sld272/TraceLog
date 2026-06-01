import { useCallback, useEffect, useState } from 'react'
import { type Post, type Comment, type PostEvent, listPosts, createPost, getPost, streamPostEvents } from '@/api/client'
import { Composer } from '@/components/Composer'
import { PostCard } from '@/components/PostCard'
import styles from './Timeline.module.css'

export function Timeline() {
  const [posts, setPosts] = useState<Post[]>([])
  const [postComments, setPostComments] = useState<Record<string, Comment[]>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchPosts = useCallback(async () => {
    try {
      const data = await listPosts(30, 0)
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

  const handleSubmit = async (content: string) => {
    const result = await createPost(content)
    /* Optimistically add the post to the top */
    const newPost: Post = {
      post_id: result.post_id,
      ts: new Date().toISOString(),
      content,
      importance: 0.5,
      comment_count: 0,
      latest_event_type: 'queued',
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
      setPosts((prev) =>
        prev.map((p) =>
          p.post_id === postId
            ? {
                ...p,
                importance: detail.post.importance,
                comment_count: detail.comments.length,
                latest_event_type: eventType ?? p.latest_event_type,
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
    } catch {
      /* silently fail for now */
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
          <p className={styles.emptyHint}>写下你的第一条想法，SOUL 们会回应你</p>
        </div>
      ) : (
        <div className={styles.feed}>
          {posts.map((post) => (
            <PostCard
              key={post.post_id}
              post={post}
              comments={postComments[post.post_id]?.map((c) => ({
                soul_name: c.soul_name,
                content: c.content,
              }))}
              onExpand={() => handleExpand(post.post_id)}
            />
          ))}
        </div>
      )}
    </div>
  )
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

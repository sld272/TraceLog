import { useCallback, useEffect, useState } from 'react'
import { type Post, type Comment, listPosts, createPost, getPost, streamPostEvents } from '@/api/client'
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
        if (event.event_type === 'comment_generated') {
          /* Refresh post detail to get comments */
          getPost(result.post_id).then((detail) => {
            setPostComments((prev) => ({
              ...prev,
              [result.post_id]: detail.comments,
            }))
            /* Update comment count */
            setPosts((prev) =>
              prev.map((p) =>
                p.post_id === result.post_id
                  ? { ...p, comment_count: detail.comments.length, latest_event_type: event.event_type }
                  : p,
              ),
            )
          })
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
      <div className={styles.loading}>
        <div className={styles.skeleton} />
        <div className={styles.skeleton} />
        <div className={styles.skeleton} />
      </div>
    )
  }

  if (error) {
    return (
      <div className={styles.error}>
        <p>无法加载时间线</p>
        <p className={styles.errorDetail}>{error}</p>
        <button className={styles.retryBtn} onClick={fetchPosts}>重试</button>
      </div>
    )
  }

  return (
    <div className={styles.timeline}>
      <Composer onSubmit={handleSubmit} />

      {posts.length === 0 ? (
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

function EmptyIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity="0.3">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
    </svg>
  )
}

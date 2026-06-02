import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import {
  type Attachment,
  type ChatMessage,
  type ChatThread,
  getChatThread,
  listChatThreads,
  sendChatMessage,
} from '@/api/client'
import { ImageGrid } from '@/components/ImageGrid'
import { ImageUploader } from '@/components/ImageUploader'
import { getSubmitShortcutTitle } from '@/utils/shortcuts'
import styles from './WorkspacePages.module.css'

interface ChatPageProps {
  soulName: string
}

export function ChatPage({ soulName }: ChatPageProps) {
  const [thread, setThread] = useState<ChatThread | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const chatInputRef = useRef<HTMLTextAreaElement>(null)
  const submitShortcutTitle = getSubmitShortcutTitle()

  const fetchThread = useCallback(async () => {
    try {
      setLoading(true)
      const threads = await listChatThreads(soulName)
      const latestThread = threads[0]
      if (!latestThread) {
        setThread(null)
        setMessages([])
        setError(null)
        return
      }
      const detail = await getChatThread(latestThread.id)
      setThread(detail.thread)
      setMessages(detail.messages)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [soulName])

  useEffect(() => {
    setDraft('')
    setAttachments([])
    fetchThread()
  }, [fetchThread])

  useEffect(() => {
    const el = chatInputRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, 200)}px`
    }
  }, [draft])

  const submitDraft = async () => {
    const body = draft.trim()
    if ((!body && attachments.length === 0) || sending) return

    const optimistic: ChatMessage = {
      id: Date.now() * -1,
      thread_id: thread?.id ?? 0,
      role: 'user',
      content: body,
      created_at: Date.now() / 1000,
      attachments,
    }
    setMessages((prev) => [...prev, optimistic])
    setDraft('')
    setAttachments([])
    setSending(true)
    try {
      const response = await sendChatMessage(soulName, body, attachments.map((attachment) => attachment.id))
      setThread(response.thread)
      setMessages(response.messages)
      setError(response.result.ok ? null : response.result.error ?? '回复失败')
    } catch (err) {
      setMessages((prev) => prev.filter((message) => message.id !== optimistic.id))
      setError(err instanceof Error ? err.message : '发送失败')
    } finally {
      setSending(false)
    }
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    submitDraft()
  }

  return (
    <div className={styles.page}>
      <div className={styles.chatShell}>
        <header className={styles.header}>
          <div className={styles.titleGroup}>
            <h1 className={styles.title}>{soulName}</h1>
            <p className={styles.subtitle}>{thread?.title ?? '私聊'}</p>
          </div>
          <button className={styles.ghostButton} onClick={fetchThread} disabled={loading || sending}>
            刷新
          </button>
        </header>

        {error && <div className={styles.notice}>{error}</div>}

        <div className={styles.messages}>
          {loading ? (
            <div className={styles.empty}>加载中...</div>
          ) : messages.length === 0 ? (
            <div className={styles.empty}>还没有消息</div>
          ) : (
            messages.map((message) => (
              <MessageBubble key={message.id} soulName={soulName} message={message} />
            ))
          )}
        </div>

        <form className={styles.chatForm} onSubmit={handleSubmit}>
          <div className={styles.chatInputGroup}>
            <textarea
              ref={chatInputRef}
              className={styles.chatInput}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault()
                  submitDraft()
                }
              }}
              placeholder={`和 ${soulName} 说点什么...`}
              disabled={sending}
              rows={2}
              aria-label="私聊消息"
            />
            <ImageUploader
              attachments={attachments}
              disabled={sending}
              onChange={setAttachments}
              showControls={false}
            />
          </div>
          <div className={styles.chatFooter}>
            {(draft.length > 0 || attachments.length > 0) && (
              <span className={styles.chatHint}>
                {draft.length} 字{attachments.length > 0 ? ` · ${attachments.length} 图` : ''}
              </span>
            )}
            <div className={styles.chatActions}>
              <ImageUploader
                attachments={attachments}
                disabled={sending}
                onChange={setAttachments}
                showPreview={false}
              />
              <span className={styles.buttonTooltipWrap} title={submitShortcutTitle}>
                <button
                  className={styles.chatSubmitButton}
                  disabled={(!draft.trim() && attachments.length === 0) || sending}
                  aria-label="发送"
                >
                  {sending ? <LoadingDots /> : <SendIcon />}
                </button>
              </span>
            </div>
          </div>
        </form>
      </div>
    </div>
  )
}

function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  )
}

function LoadingDots() {
  return (
    <span className={styles.loadingDots}>
      <span className={styles.dot} />
      <span className={styles.dot} />
      <span className={styles.dot} />
    </span>
  )
}

function MessageBubble({ soulName, message }: { soulName: string; message: ChatMessage }) {
  const isUser = message.role === 'user'
  return (
    <article className={`${styles.message} ${isUser ? styles.messageUser : styles.messageAssistant}`}>
      <span className={styles.messageRole}>{isUser ? '我' : soulName}</span>
      {message.content}
      <ImageGrid attachments={message.attachments ?? []} />
    </article>
  )
}

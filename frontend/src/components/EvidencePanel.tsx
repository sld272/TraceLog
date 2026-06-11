import { useMemo, useState } from 'react'
import {
  type EvidenceChannel,
  type EvidenceItem,
  parseMessageEvidence,
  submitEvidenceFeedback,
} from '@/api/client'
import { formatRoute } from '@/router'
import styles from './EvidencePanel.module.css'

interface EvidencePanelProps {
  metadata?: string | null
  channel: EvidenceChannel
  messageId: number
  compact?: boolean
}

export function EvidencePanel({ metadata, channel, messageId, compact = false }: EvidencePanelProps) {
  const evidence = useMemo(() => parseMessageEvidence(metadata), [metadata])
  const [marked, setMarked] = useState<Set<string>>(() => new Set())
  const [pendingDocId, setPendingDocId] = useState<string | null>(null)

  if (messageId <= 0 || evidence.length === 0) return null

  const markIrrelevant = async (docId: string) => {
    if (marked.has(docId) || pendingDocId) return
    setPendingDocId(docId)
    try {
      await submitEvidenceFeedback(channel, messageId, docId)
      setMarked((prev) => new Set(prev).add(docId))
    } finally {
      setPendingDocId(null)
    }
  }

  return (
    <details className={`${styles.panel} ${compact ? styles.compact : ''}`}>
      <summary className={styles.summary}>
        <span className={styles.summaryText}>引用记忆</span>
        <span className={styles.count}>×{evidence.length}</span>
      </summary>
      <div className={styles.items}>
        {evidence.map((item) => (
          <EvidenceRow
            key={item.doc_id}
            item={item}
            marked={marked.has(item.doc_id)}
            pending={pendingDocId === item.doc_id}
            onMarkIrrelevant={() => markIrrelevant(item.doc_id)}
          />
        ))}
      </div>
    </details>
  )
}

function EvidenceRow({
  item,
  marked,
  pending,
  onMarkIrrelevant,
}: {
  item: EvidenceItem
  marked: boolean
  pending: boolean
  onMarkIrrelevant: () => void
}) {
  const clickable = item.post_id !== null
  const title = clickable ? item.snippet : '来自私聊的记忆片段，暂不支持跳转'
  return (
    <div className={`${styles.item} ${marked ? styles.marked : ''}`}>
      <button
        className={`${styles.itemMain} ${clickable ? styles.clickable : ''}`}
        onClick={() => {
          if (item.post_id) {
            window.location.hash = formatRoute({ kind: 'post', postId: item.post_id, highlight: item.doc_id })
          }
        }}
        disabled={!clickable}
        title={title}
      >
        <span className={styles.badge}>{typeLabel(item.type)}</span>
        <span className={styles.snippet}>{item.snippet}</span>
      </button>
      <button
        className={styles.feedbackButton}
        onClick={onMarkIrrelevant}
        disabled={marked || pending}
        title={marked ? '已标记不相关' : '标记这条不相关'}
        aria-label="标记这条不相关"
      >
        {marked ? '已标记' : '×'}
      </button>
    </div>
  )
}

function typeLabel(type: string): string {
  if (type === 'post') return '记录'
  if (type === 'post_vision') return '图片'
  if (type === 'comment') return '评论'
  if (type === 'chat') return '私聊'
  return '记忆'
}

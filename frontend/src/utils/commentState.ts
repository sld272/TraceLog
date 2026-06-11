import {
  type Attachment,
  type Comment,
  type CommentConversation,
  type CommentMessage,
  type PostEvent,
} from '@/api/client'
import { type CommentConversationState } from '@/components/PostCard'

export function toConversationState(
  conversation: CommentConversation,
  messages: CommentMessage[],
): CommentConversationState {
  return {
    conversation,
    messages,
    sending: false,
    error: null,
  }
}

export function failedCommentState(
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

export function buildSendingCommentState(
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

export function withPendingCommentRerun(
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

export function shouldRefreshPostDetail(event: PostEvent): boolean {
  return [
    'reply_succeeded',
    'reply_failed',
    'light_reflection_succeeded',
    'pipeline_done',
  ].includes(event.event_type)
}

export function latestEventId(events: PostEvent[]): number {
  return events.reduce((latest, event) => Math.max(latest, event.id), 0)
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

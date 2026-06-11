export type PostMutationKind = 'updated' | 'deleted'

export interface PostMutationSignal {
  postId: string
  kind: PostMutationKind
  nonce: number
}

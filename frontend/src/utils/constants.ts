/** API 相关常量 */
export const API_LIMITS = {
  /** 默认帖子列表数量 */
  POSTS_DEFAULT: 30,
  /** 默认消息历史数量 */
  MESSAGES_DEFAULT: 30,
  /** 默认修订历史数量 */
  REVISIONS_DEFAULT: 20,
  /** 反思预览数量 */
  REFLECTION_PREVIEW: 20,
  /** 反思处理上限 */
  REFLECTION_LIMIT: 100,
} as const

/** 图片上传相关常量 */
export const IMAGE_UPLOAD = {
  /** 最大图片数量 */
  MAX_COUNT: 9,
  /** 允许的 MIME 类型 */
  ALLOWED_TYPES: new Set(['image/jpeg', 'image/png']),
  /** 允许的文件扩展名 */
  ALLOWED_EXTENSIONS: new Set(['jpg', 'jpeg', 'png']),
} as const

/** UI 布局相关常量 */
export const LAYOUT = {
  /** 评论回复框左边距（与头像对齐） */
  COMMENT_INDENT: 42,
  /** 文本区域最大高度 */
  TEXTAREA_MAX_HEIGHT: 200,
  /** 回复框文本区域最大高度 */
  REPLY_TEXTAREA_MAX_HEIGHT: 120,
} as const

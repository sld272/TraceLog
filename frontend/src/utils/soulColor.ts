/** SOUL 视觉色板：预选 10 个观感可控的色相，按名字哈希取索引。
 *  比 charCode 求和取模更抗撞色（异序同字符不再同色），且避开浑浊色区。 */
const SOUL_HUES = [14, 36, 152, 174, 200, 222, 256, 286, 330, 354] as const

export interface SoulColors {
  /** 评论气泡底色 */
  tint: string
  /** 头像底色 */
  badgeBackground: string
  /** 头像文字色 */
  badgeText: string
}

export function soulColors(name: string): SoulColors {
  const hue = SOUL_HUES[fnv1a(name) % SOUL_HUES.length] ?? SOUL_HUES[0]
  return {
    tint: `hsl(${hue}, 30%, 97%)`,
    badgeBackground: `hsl(${hue}, 35%, 88%)`,
    badgeText: `hsl(${hue}, 40%, 35%)`,
  }
}

function fnv1a(value: string): number {
  let hash = 0x811c9dc5
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return hash >>> 0
}

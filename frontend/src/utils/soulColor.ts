/** SOUL 视觉色板：预选 10 个观感可控的色相，按名字哈希取索引。
 *  比 charCode 求和取模更抗撞色（异序同字符不再同色），且避开浑浊色区。
 *
 *  单独哈希只能保证"同名同色"，不能保证"异名异色"——10 个色槽下 4 个 SOUL
 *  就有约一半概率撞色（内置的拾迹者和毒舌好友开箱即撞）。所以对已知的 SOUL
 *  集合用 assignSoulHues 做碰撞解决：哈希定首选槽位，被占则线性探测下一个
 *  空槽。组件侧通过 SoulColorContext 消费该分配结果。 */
const SOUL_HUES = [14, 36, 152, 174, 200, 222, 256, 286, 330, 354] as const

export interface SoulColors {
  /** 评论气泡底色 */
  tint: string
  /** 头像底色 */
  badgeBackground: string
  /** 头像文字色 */
  badgeText: string
}

/** 无集合信息时的退路：纯按名字哈希取色，同名稳定但异名可能撞色。 */
export function soulColors(name: string): SoulColors {
  return colorsFromHue(SOUL_HUES[fnv1a(name) % SOUL_HUES.length] ?? SOUL_HUES[0])
}

/** 为一组 SOUL 名字分配互不重复的色相（超过色板容量后允许复用）。
 *  按名字码点排序后处理，结果只取决于集合成员、与传入顺序和 sort_order 无关，
 *  这样调整人格排序不会导致头像变色。 */
export function assignSoulHues(names: string[]): Map<string, number> {
  const assigned = new Map<string, number>()
  const taken = new Set<number>()
  const uniqueSorted = [...new Set(names)].sort()
  for (const name of uniqueSorted) {
    const preferred = fnv1a(name) % SOUL_HUES.length
    let index = preferred
    if (taken.size < SOUL_HUES.length) {
      while (taken.has(index)) {
        index = (index + 1) % SOUL_HUES.length
      }
    }
    taken.add(index)
    assigned.set(name, SOUL_HUES[index] ?? SOUL_HUES[0])
  }
  return assigned
}

export function colorsFromHue(hue: number): SoulColors {
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

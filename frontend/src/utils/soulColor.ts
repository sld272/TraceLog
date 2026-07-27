/** SOUL 视觉色板。
 *
 *  颜色在这套界面里只有一个作用：让你一眼认出是谁在说话。所以色板不是"十个色相
 *  均匀铺开"，而是一组同明度、低饱和的印色 —— 像盖在纸上的印章，任意几个同屏
 *  出现都不会打架，也不会把注意力从文字上抢走。
 *
 *  单独哈希只能保证"同名同色"，不能保证"异名异色"。所以对已知的 SOUL 集合用
 *  assignSoulSlots 做碰撞解决：哈希定首选槽位，被占则线性探测下一个空槽。
 *  组件侧通过 SoulColorContext 消费该分配结果。 */
const SOUL_INKS = [
  { solid: '#3d8a78', tint: '#ecf4f1' }, // 松绿
  { solid: '#4a6d94', tint: '#edf1f6' }, // 靛青
  { solid: '#a86c42', tint: '#f7f0e9' }, // 赭石
  { solid: '#7d5f83', tint: '#f3eff4' }, // 紫褐
  { solid: '#6b8050', tint: '#f0f4ea' }, // 苔绿
  { solid: '#a55b52', tint: '#f7efed' }, // 砖红
  { solid: '#42768c', tint: '#ecf2f5' }, // 石青
  { solid: '#907a38', tint: '#f5f2e7' }, // 暗金
  { solid: '#5a787e', tint: '#eef2f3' }, // 灰蓝
  { solid: '#8a6350', tint: '#f5efeb' }, // 胡桃
] as const

export interface SoulColors {
  /** 评论气泡底色 */
  tint: string
  /** 头像底色 */
  badgeBackground: string
  /** 头像文字色 */
  badgeText: string
  /** 需要用 SOUL 本色写字时用（名字、细描边），保证在纸底上够暗 */
  accent: string
}

/** 无集合信息时的退路：纯按名字哈希取色，同名稳定但异名可能撞色。 */
export function soulColors(name: string): SoulColors {
  return colorsFromSlot(fnv1a(name) % SOUL_INKS.length)
}

/** 为一组 SOUL 名字分配互不重复的色槽（超过色板容量后允许复用）。
 *  按名字码点排序后处理，结果只取决于集合成员、与传入顺序和 sort_order 无关，
 *  这样调整人格排序不会导致头像变色。 */
export function assignSoulSlots(names: string[]): Map<string, number> {
  const assigned = new Map<string, number>()
  const taken = new Set<number>()
  const uniqueSorted = [...new Set(names)].sort()
  for (const name of uniqueSorted) {
    const preferred = fnv1a(name) % SOUL_INKS.length
    let index = preferred
    if (taken.size < SOUL_INKS.length) {
      while (taken.has(index)) {
        index = (index + 1) % SOUL_INKS.length
      }
    }
    taken.add(index)
    assigned.set(name, index)
  }
  return assigned
}

export function colorsFromSlot(slot: number): SoulColors {
  const ink = SOUL_INKS[slot % SOUL_INKS.length] ?? SOUL_INKS[0]
  return {
    tint: ink.tint,
    badgeBackground: ink.solid,
    badgeText: '#fffefb',
    accent: ink.solid,
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

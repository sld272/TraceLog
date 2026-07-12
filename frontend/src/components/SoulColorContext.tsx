import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { assignSoulHues, colorsFromHue, soulColors, type SoulColors } from '@/utils/soulColor'

/** 已知 SOUL 集合的去撞色分配表（name → hue）。空表示集合未加载。 */
const SoulColorContext = createContext<Map<string, number>>(new Map())

/** 用当前 SOUL 名字集合建立配色分配，保证集合内互不撞色。 */
export function SoulColorProvider({ soulNames, children }: { soulNames: string[]; children: ReactNode }) {
  // join 出稳定 key，避免父组件每次渲染新数组导致的重复计算
  const namesKey = soulNames.join('\n')
  const assignments = useMemo(
    () => assignSoulHues(namesKey ? namesKey.split('\n') : []),
    [namesKey],
  )
  return <SoulColorContext.Provider value={assignments}>{children}</SoulColorContext.Provider>
}

/** 取一个 SOUL 的配色：优先用集合内去撞色的分配，不在集合中（如已禁用、
 *  集合尚未加载）时退回纯哈希，保证任何名字都有稳定颜色。 */
export function useSoulColors(name: string): SoulColors {
  const assignments = useContext(SoulColorContext)
  const hue = assignments.get(name)
  return hue === undefined ? soulColors(name) : colorsFromHue(hue)
}

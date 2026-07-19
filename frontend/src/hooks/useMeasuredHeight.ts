import { useEffect, useRef, useState, type RefObject } from 'react'

/** 用 ResizeObserver 跟踪元素内容高度，供「按可用空间决定渲染条数」的场景使用。 */
export function useMeasuredHeight<T extends HTMLElement>(): [RefObject<T | null>, number] {
  const ref = useRef<T>(null)
  const [height, setHeight] = useState(0)

  useEffect(() => {
    const element = ref.current
    if (!element) return
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry) setHeight(entry.contentRect.height)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return [ref, height]
}

import { useState } from 'react'
import styles from './DiagnosticsButton.module.css'

interface DiagnosticsButtonProps {
  /** 出错的地方在做什么，用一句人话写，方便对方一眼知道是哪一步。 */
  context: string
  /** 原始报错，通常是英文。展开前不出现在界面上。 */
  detail: string | null | undefined
}

/** 失败提示旁边的小按钮：点开看诊断信息，同时复制到剪贴板。
 *
 * 界面上默认不摆原始报错——一段英文 traceback 突然出现在眼前，比"请稍后重试"更
 * 让人慌。但真出了事，用户需要的也不是看懂它，而是能把它交给看得懂的人，所以点
 * 开的同时就复制好，他直接粘出去即可。 */
export function DiagnosticsButton({ context, detail }: DiagnosticsButtonProps) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const text = diagnosticsText(context, detail)

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (!next) return
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
    } catch {
      /* 剪贴板不可用（非安全上下文等）时，展开的内容照样能手动选中复制 */
      setCopied(false)
    }
  }

  return (
    <div className={styles.wrap}>
      <button type="button" className={styles.toggle} onClick={toggle} aria-expanded={open}>
        {open ? (copied ? '诊断信息（已复制）' : '诊断信息') : '诊断信息'}
      </button>
      {open && <pre className={styles.detail}>{text}</pre>}
    </div>
  )
}

function diagnosticsText(context: string, detail: string | null | undefined): string {
  const lines = [
    `时间：${new Date().toLocaleString('zh-CN')}`,
    `位置：${context}`,
    `报错：${(detail ?? '').trim() || '（无）'}`,
  ]
  return lines.join('\n')
}

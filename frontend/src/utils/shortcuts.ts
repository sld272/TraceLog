export function getSubmitShortcutTitle() {
  return `发送（${getSubmitShortcutKeys()}）`
}

function getSubmitShortcutKeys() {
  return `${isApplePlatform() ? 'Cmd' : 'Ctrl'}+Enter`
}

function isApplePlatform() {
  if (typeof navigator === 'undefined') return false

  const nav = navigator as Navigator & {
    userAgentData?: { platform?: string }
  }
  const platform = nav.userAgentData?.platform ?? navigator.platform ?? ''

  return /Mac|iPhone|iPad|iPod/i.test(platform)
}

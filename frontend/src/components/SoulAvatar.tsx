import { soulColors } from '@/utils/soulColor'

/** SOUL 首字母头像。尺寸与形状由调用方的 className 决定，颜色统一从色板取。 */
export function SoulAvatar({ name, className }: { name: string; className?: string }) {
  const colors = soulColors(name)
  return (
    <span
      className={className}
      style={{ backgroundColor: colors.badgeBackground, color: colors.badgeText }}
      aria-hidden="true"
    >
      {name.charAt(0).toUpperCase()}
    </span>
  )
}

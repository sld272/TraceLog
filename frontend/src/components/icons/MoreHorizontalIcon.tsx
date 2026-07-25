interface IconProps {
  width?: number
  height?: number
}

export function MoreHorizontalIcon({ width = 13, height = 13 }: IconProps = {}) {
  return (
    <svg
      width={width}
      height={height}
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <circle cx="5" cy="12" r="1.9" />
      <circle cx="12" cy="12" r="1.9" />
      <circle cx="19" cy="12" r="1.9" />
    </svg>
  )
}

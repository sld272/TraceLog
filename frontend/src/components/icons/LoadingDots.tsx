import styles from './LoadingDots.module.css'

export function LoadingDots() {
  return (
    <span className={styles.loadingDots}>
      <span className={styles.dot} />
      <span className={styles.dot} />
      <span className={styles.dot} />
    </span>
  )
}

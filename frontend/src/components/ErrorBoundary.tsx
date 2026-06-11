import { Component, type ErrorInfo, type ReactNode } from 'react'
import styles from './ErrorBoundary.module.css'

interface ErrorBoundaryProps {
  children: ReactNode
  title?: string
  message?: string
  showDetails?: boolean
  variant?: 'page' | 'inline'
}

interface ErrorBoundaryState {
  error: Error | null
  errorInfo: ErrorInfo | null
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = {
    error: null,
    errorInfo: null,
  }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    this.setState({ errorInfo })
    console.error(error, errorInfo)
  }

  render() {
    const { error, errorInfo } = this.state
    if (!error) return this.props.children

    if (this.props.variant === 'inline') {
      return (
        <div className={styles.inline} role="alert">
          <strong>{this.props.title ?? '此条内容无法显示'}</strong>
          {this.props.message && <span>{this.props.message}</span>}
        </div>
      )
    }

    return (
      <main className={styles.page} role="alert">
        <section className={styles.panel}>
          <h1>{this.props.title ?? '界面出错了'}</h1>
          <p>{this.props.message ?? '这不是你的数据出了问题，只是当前界面渲染失败。可以重试或刷新页面。'}</p>
          <div className={styles.actions}>
            <button onClick={() => this.setState({ error: null, errorInfo: null })}>重试</button>
            <button onClick={() => window.location.reload()}>刷新页面</button>
          </div>
          {this.props.showDetails && (
            <details className={styles.details}>
              <summary>错误详情</summary>
              <pre>{formatError(error, errorInfo)}</pre>
            </details>
          )}
        </section>
      </main>
    )
  }
}

function formatError(error: Error, errorInfo: ErrorInfo | null): string {
  return [error.stack || error.message, errorInfo?.componentStack].filter(Boolean).join('\n\n')
}

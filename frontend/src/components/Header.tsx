import SymbolSearch from './SymbolSearch'
import { INTERVALS, type Interval } from '../types'
import type { WsStatus } from '../ws/binanceWs'

interface Props {
  symbol: string
  interval: Interval
  wsStatus: WsStatus
  polling: boolean
  alertsEnabled: boolean
  onToggleAlerts: () => void
  onSymbol: (s: string) => void
  onInterval: (i: Interval) => void
}

export default function Header({
  symbol,
  interval,
  wsStatus,
  polling,
  alertsEnabled,
  onToggleAlerts,
  onSymbol,
  onInterval,
}: Props) {
  const showPolling = polling && wsStatus === 'closed'
  const statusText = showPolling
    ? '轮询 60s'
    : wsStatus === 'open'
      ? '实时'
      : wsStatus === 'connecting'
        ? '连接中'
        : '已断开'
  const statusClass = showPolling ? 'ws-polling' : `ws-${wsStatus}`
  return (
    <header className="header">
      <div className="brand">
        <span className="brand-logo">◉</span> CoinLens
      </div>
      <SymbolSearch symbol={symbol} onSelect={onSymbol} />
      <div className="interval-group">
        {INTERVALS.map((i) => (
          <button
            key={i}
            className={`interval-btn ${interval === i ? 'active' : ''}`}
            onClick={() => onInterval(i)}
          >
            {i}
          </button>
        ))}
      </div>
      <button
        className={`alert-btn ${alertsEnabled ? 'alert-on' : ''}`}
        onClick={onToggleAlerts}
        title="本地预警：触及关键位 / 扫流动性 / CHoCH 时通知（浏览器通知 + 站内提示）"
      >
        <span className="alert-bell">🔔</span>
        预警 {alertsEnabled ? '开' : '关'}
      </button>
      <div className={`ws-status ${statusClass}`} title="行情数据状态">
        <span className="ws-dot" />
        {statusText}
      </div>
    </header>
  )
}

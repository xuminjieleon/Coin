import SymbolSearch from './SymbolSearch'
import { INTERVALS, type Interval } from '../types'

interface Props {
  symbol: string
  interval: Interval
  refreshing: boolean
  autoRefresh: boolean
  lastRefresh: Date | null
  onRefresh: () => void
  onToggleAutoRefresh: () => void
  alertsEnabled: boolean
  onToggleAlerts: () => void
  onSymbol: (s: string) => void
  onInterval: (i: Interval) => void
  onScan: () => void
}

function formatTime(d: Date | null): string {
  if (!d) return '—'
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  const s = String(d.getSeconds()).padStart(2, '0')
  return `${h}:${m}:${s}`
}

export default function Header({
  symbol,
  interval,
  refreshing,
  autoRefresh,
  lastRefresh,
  onRefresh,
  onToggleAutoRefresh,
  alertsEnabled,
  onToggleAlerts,
  onSymbol,
  onInterval,
  onScan,
}: Props) {
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
        className={`refresh-btn ${refreshing ? 'refresh-btn-busy' : ''}`}
        onClick={onRefresh}
        disabled={refreshing}
        title="手动刷新：K线、分析、衍生品、回测全部重新拉取"
      >
        <span className={`refresh-icon${refreshing ? ' refresh-spinning' : ''}`}>⟳</span>
        {refreshing ? '刷新中…' : '刷新'}
      </button>
      <button
        className={`refresh-btn ${autoRefresh ? 'auto-on' : ''}`}
        onClick={onToggleAutoRefresh}
        title="自动刷新：每 5 分钟重新拉取全部数据（关闭后仅手动刷新）"
      >
        自动 5min {autoRefresh ? '开' : '关'}
      </button>
      <button
        className="refresh-btn scan-btn"
        onClick={onScan}
        title="全市场扫描：按 24h 成交额前 40 的 USDT 交易对跑同一套引擎评分排序，点击行切换标的"
      >
        <span>⚡</span> 扫描
      </button>
      <button
        className={`alert-btn ${alertsEnabled ? 'alert-on' : ''}`}
        onClick={onToggleAlerts}
        title="本地预警：触及关键位 / 扫流动性 / CHoCH 时通知（浏览器通知 + 站内提示）"
      >
        <span className="alert-bell">🔔</span>
        预警 {alertsEnabled ? '开' : '关'}
      </button>
      <div className="ws-status" title="数据更新时间（手动 / 每 5 分钟自动刷新，不使用实时推送）">
        更新于 {formatTime(lastRefresh)}
      </div>
    </header>
  )
}

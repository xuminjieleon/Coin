import type { Liquidations } from '../api/client'
import { formatUsd, formatPrice } from '../utils/format'
import SourceHint, { type SourceLink } from './SourceHint'

interface Props {
  data: Liquidations | null
  symbol: string
}

function links(symbol: string): SourceLink[] {
  const base = symbol.replace(/USDT$/, '')
  return [
    { label: 'Coinglass 清算地图', url: `https://www.coinglass.com/LiquidationData/${base}` },
    { label: 'Coinglass 多空清算比', url: 'https://www.coinglass.com/LiquidationHeatMap' },
  ]
}

function LiqChart({ history }: { history: Liquidations['history'] }) {
  if (history.length === 0) return null
  const max = Math.max(...history.map((h) => Math.max(h.longUsd, h.shortUsd)), 1)
  const w = 100 / history.length
  return (
    <svg className="liq-chart" viewBox="0 0 100 40" preserveAspectRatio="none">
      {history.map((h, i) => {
        const hl = (h.longUsd / max) * 20
        const hs = (h.shortUsd / max) * 20
        return (
          <g key={h.time}>
            <rect x={i * w + w * 0.1} y={20 - hl} width={w * 0.35} height={Math.max(hl, 0.2)} className="liq-bar-long" />
            <rect x={i * w + w * 0.5} y={20} width={w * 0.35} height={Math.max(hs, 0.2)} className="liq-bar-short" />
          </g>
        )
      })}
      <line x1="0" y1="20" x2="100" y2="20" className="funding-chart-zero" />
    </svg>
  )
}

export default function LiquidationPanel({ data, symbol }: Props) {
  const hint =
    '24h 多空清算金额来自 Gate.io 合约统计（每小时清算 USD 名义值）；百分位=今日累计相对本地持久化的近一年日清算分布。估算强平位=以现价开仓的隔离保证金近似（未计维持保证金），是杠杆地图而非真实清算簇。'
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">清算数据</div>
        <span className="source-badge">Gate.io</span>
        <SourceHint text={hint} links={links(symbol)} />
      </div>
      {!data ? (
        <div className="panel-empty">加载中…</div>
      ) : data.source == null && data.estimated.length === 0 ? (
        <div className="panel-empty">{data.note}</div>
      ) : (
        <>
          {data.source == null && <div className="ob-note">{data.note}</div>}
          {data.total24hUsd != null && (
            <div className="deriv-grid">
              <div className="deriv-item">
                <div className="deriv-label">24h 多头清算</div>
                <div className="deriv-value text-down">{formatUsd(data.long24hUsd)}</div>
                <div className="deriv-sub">多头爆仓</div>
              </div>
              <div className="deriv-item">
                <div className="deriv-label">24h 空头清算</div>
                <div className="deriv-value text-up">{formatUsd(data.short24hUsd)}</div>
                <div className="deriv-sub">
                  多空清算比 {data.longShortRatio != null ? data.longShortRatio.toFixed(2) : '--'}
                </div>
              </div>
              <div className="deriv-item">
                <div className="deriv-label">清算烈度</div>
                <div className="deriv-value">
                  {data.percentileVsYear != null ? `${data.percentileVsYear.toFixed(0)}%` : '--'}
                </div>
                <div className="deriv-sub">
                  {data.percentileVsYear != null && data.percentileVsYear > 70
                    ? '接近一年高位（清算瀑布风险时段）'
                    : '相对一年分布'}
                </div>
              </div>
            </div>
          )}
          {data.history.length > 0 && data.source != null && (
            <div className="funding-chart-wrap">
              <div className="section-label">近 48h 每小时清算（上=多头 红 / 下=空头 绿）</div>
              <LiqChart history={data.history} />
            </div>
          )}
          {data.estimated.length > 0 && (
            <div className="liq-map">
              <div className="section-label">估算强平位（以现价 {formatPrice(data.price)} 开仓）</div>
              <div className="ob-band-row ob-band-head">
                <span>杠杆</span>
                <span>多头强平</span>
                <span>空头强平</span>
              </div>
              {data.estimated.map((e) => (
                <div className="ob-band-row" key={e.leverage}>
                  <span className="ob-band-pct">{e.leverage}×</span>
                  <span className="text-down">{formatPrice(e.longLiq)}</span>
                  <span className="text-up">{formatPrice(e.shortLiq)}</span>
                </div>
              ))}
            </div>
          )}
          {data.source != null && <div className="pos-note">{data.note}</div>}
        </>
      )}
    </div>
  )
}

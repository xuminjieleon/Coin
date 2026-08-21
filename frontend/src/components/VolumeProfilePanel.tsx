import type { VolumeProfile } from '../api/client'
import { formatPrice } from '../utils/format'
import SourceHint from './SourceHint'

interface Props {
  profile: VolumeProfile | null
  currentPrice: number | null
  symbol: string
}

const HEIGHT = 260
const LABEL_W = 70
const BAR_AREA_W = 150

export default function VolumeProfilePanel({ profile, currentPrice, symbol }: Props) {
  if (!profile || profile.bins.length === 0) {
    return (
      <div className="panel">
        <div className="panel-title-row">
          <div className="panel-title">成交量分布</div>
          <SourceHint
            text="基于最近 300 根 K 线本地计算的成交量分布（VPVR）：POC 为成交最密集价位，VAH/VAL 为覆盖 70% 成交量的价值区上下沿。数据源为币安现货镜像。"
            links={[
              {
                label: 'TradingView 图表',
                url: `https://www.tradingview.com/chart/?symbol=BINANCE:${symbol}`,
              },
            ]}
          />
        </div>
        <div className="panel-empty">加载中…</div>
      </div>
    )
  }

  const bins = [...profile.bins].sort((a, b) => b.priceHigh - a.priceHigh)
  const maxVol = Math.max(...bins.map((b) => b.volume), 1e-9)
  const rowH = HEIGHT / bins.length
  const topPrice = bins[0].priceHigh
  const bottomPrice = bins[bins.length - 1].priceLow
  const priceSpan = topPrice - bottomPrice || 1

  const yOfPrice = (p: number) => ((topPrice - p) / priceSpan) * HEIGHT

  const pocIndex = bins.findIndex((b) => (b.priceLow + b.priceHigh) / 2 === profile.poc)
  const vahY = yOfPrice(profile.vah)
  const valY = yOfPrice(profile.val)
  const curY = currentPrice != null ? yOfPrice(currentPrice) : null

  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">成交量分布</div>
        <SourceHint
          text="基于最近 300 根 K 线本地计算的成交量分布（VPVR）：POC 为成交最密集价位，VAH/VAL 为覆盖 70% 成交量的价值区上下沿。数据源为币安现货镜像。"
          links={[
            {
              label: 'TradingView 图表',
              url: `https://www.tradingview.com/chart/?symbol=BINANCE:${symbol}`,
            },
          ]}
        />
      </div>
      <div className="vp-stats">
        <div className="vp-stat">
          <span className="vp-stat-label">POC</span>
          <span className="vp-stat-value vp-poc">{formatPrice(profile.poc)}</span>
        </div>
        <div className="vp-stat">
          <span className="vp-stat-label">动态POC</span>
          <span className="vp-stat-value vp-dev-poc">
            {profile.developingPoc != null ? formatPrice(profile.developingPoc) : '--'}
          </span>
        </div>
        <div className="vp-stat">
          <span className="vp-stat-label">VAH</span>
          <span className="vp-stat-value">{formatPrice(profile.vah)}</span>
        </div>
        <div className="vp-stat">
          <span className="vp-stat-label">VAL</span>
          <span className="vp-stat-value">{formatPrice(profile.val)}</span>
        </div>
      </div>
      <svg
        className="vp-chart"
        viewBox={`0 0 ${LABEL_W + BAR_AREA_W} ${HEIGHT}`}
        preserveAspectRatio="none"
      >
        {/* value area band */}
        <rect
          x={LABEL_W}
          y={Math.min(vahY, valY)}
          width={BAR_AREA_W}
          height={Math.abs(valY - vahY)}
          className="vp-va-band"
        />
        {/* bars */}
        {bins.map((b, i) => {
          const w = (b.volume / maxVol) * BAR_AREA_W
          const isPoc = i === pocIndex
          return (
            <rect
              key={i}
              x={LABEL_W}
              y={i * rowH}
              width={Math.max(w, 0.5)}
              height={Math.max(rowH - 0.5, 0.5)}
              className={isPoc ? 'vp-bar-poc' : 'vp-bar'}
            />
          )
        })}
        {/* current price line */}
        {curY != null && curY >= 0 && curY <= HEIGHT && (
          <line
            x1={LABEL_W}
            y1={curY}
            x2={LABEL_W + BAR_AREA_W}
            y2={curY}
            className="vp-current-line"
          />
        )}
      </svg>
    </div>
  )
}

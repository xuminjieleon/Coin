import type { OrderBook } from '../api/client'
import { formatUsd, formatPrice } from '../utils/format'
import SourceHint, { type SourceLink } from './SourceHint'

interface Props {
  data: OrderBook | null
  symbol: string
}

function links(symbol: string): SourceLink[] {
  const base = symbol.replace(/USDT$/, '')
  return [
    { label: 'Gate.io 合约盘口', url: `https://www.gate.io/futures/USDT/${base}_${'USDT'}` },
    { label: 'Coinglass 订单簿', url: 'https://www.coinglass.com/OrderBook' },
  ]
}

function ImbalanceBar({ value }: { value: number | null }) {
  if (value == null) return <span className="ob-imb-na">--</span>
  const pct = Math.min(100, Math.abs(value) * 100)
  return (
    <div className="ob-imb">
      <div className="ob-imb-track">
        <div
          className={`ob-imb-fill ${value >= 0 ? 'ob-imb-bid' : 'ob-imb-ask'}`}
          style={{ width: `${pct / 2}%`, [value >= 0 ? 'marginRight' : 'marginLeft']: 'auto' } as React.CSSProperties}
        />
      </div>
      <span className={`ob-imb-val ${value >= 0 ? 'text-up' : 'text-down'}`}>
        {value >= 0 ? '+' : ''}
        {(value * 100).toFixed(0)}%
      </span>
    </div>
  )
}

export default function OrderBookPanel({ data, symbol }: Props) {
  const hint =
    '订单簿快照（刷新时拉取，非实时流）。来源按优先级自动探测：币安合约盘 → Gate.io 合约盘 → 币安现货盘（响应标注实际来源）。指标：点差、±0.1%~1% 价格带内买卖深度与失衡度（正=买盘厚）、大单墙（单档挂单 > 同带中位数 5 倍）。'
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">订单簿与微观结构</div>
        {data?.source === 'binance_perp' && <span className="source-badge">币安合约</span>}
        {data?.source === 'gateio_perp' && <span className="source-badge">Gate.io 合约</span>}
        {data?.source === 'binance_spot' && <span className="source-badge">币安现货</span>}
        <SourceHint text={hint} links={links(symbol)} />
      </div>
      {!data || data.mid == null ? (
        <div className="panel-empty">{data?.note ?? '加载中…'}</div>
      ) : (
        <>
          <div className="ob-head">
            <span>买一 {formatPrice(data.bestBid)}</span>
            <span>卖一 {formatPrice(data.bestAsk)}</span>
            <span>点差 {data.spreadBps != null ? `${data.spreadBps.toFixed(2)} bps` : '--'}</span>
          </div>
          <div className="ob-band-row ob-band-head">
            <span>价格带</span>
            <span>买侧深度</span>
            <span>卖侧深度</span>
            <span>失衡</span>
          </div>
          {(data.bands ?? []).map((b) => (
            <div className="ob-band-row" key={b.bandPct}>
              <span className="ob-band-pct">±{b.bandPct}%</span>
              <span>{formatUsd(b.bidUsd)}</span>
              <span>{formatUsd(b.askUsd)}</span>
              <ImbalanceBar value={b.imbalance} />
            </div>
          ))}
          {data.walls && data.walls.length > 0 && (
            <div className="ob-walls">
              <div className="section-label">大单墙（±1% 内，USD 名义）</div>
              {data.walls.map((w, i) => (
                <div className="ob-wall-row" key={i}>
                  <span className={w.side === 'bid' ? 'text-up' : 'text-down'}>
                    {w.side === 'bid' ? '买墙' : '卖墙'}
                  </span>
                  <span>{formatPrice(w.price)}</span>
                  <span>{formatUsd(w.usd)}</span>
                  <span className="text-dim">{w.distBps.toFixed(0)} bps</span>
                </div>
              ))}
            </div>
          )}
          {data.note && <div className="ob-note">{data.note}</div>}
        </>
      )}
    </div>
  )
}

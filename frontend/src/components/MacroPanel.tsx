import type { MacroResponse, MacroSeries } from '../api/client'
import { formatPct } from '../utils/format'
import SourceHint, { type SourceLink } from './SourceHint'

interface Props {
  data: MacroResponse | null
}

const LINKS: SourceLink[] = [
  { label: 'Yahoo Finance', url: 'https://finance.yahoo.com' },
  { label: 'TradingView DXY', url: 'https://www.tradingview.com/symbols/TVC-DXY/' },
]

function Spark({ points }: { points: number[] }) {
  if (points.length < 2) return null
  const min = Math.min(...points)
  const max = Math.max(...points)
  const span = max - min || 1
  const d = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${(i / (points.length - 1)) * 100} ${28 - ((p - min) / span) * 24}`)
    .join(' ')
  const up = points[points.length - 1] >= points[0]
  return (
    <svg className="macro-spark" viewBox="0 0 100 30" preserveAspectRatio="none">
      <path d={d} className={up ? 'macro-spark-up' : 'macro-spark-down'} />
    </svg>
  )
}

function corrColor(c: number | null): string {
  if (c == null) return ''
  const abs = Math.abs(c)
  if (abs >= 0.7) return c > 0 ? 'text-down-strong' : 'text-up-strong'
  if (abs >= 0.4) return c > 0 ? 'text-down' : 'text-up'
  return ''
}

export default function MacroPanel({ data }: Props) {
  const hint =
    'BTC 与宏观资产（纳指/美元指数/黄金/VIX/10Y 收益率/MSTR/COIN）的联动：日线收盘（Yahoo，本地缓存）、30/60/90 日收益相关性、60 日 beta。高相关时段（|corr|≥0.7）加密独立行情概率下降，宏观事件（FOMC/CPI）冲击会被放大。'
  const byKey = new Map((data?.correlations ?? []).map((c) => [c.key, c]))
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">宏观联动</div>
        <span className="source-badge">{(data?.source ?? '').split('（')[0] || '--'}</span>
        <SourceHint text={hint} links={LINKS} />
      </div>
      {!data ? (
        <div className="panel-empty">加载中…（首拉宏观序列约 10-15 秒）</div>
      ) : (
        <>
          <div className="macro-table">
            <div className="macro-row macro-head">
              <span>资产</span>
              <span>最新</span>
              <span>1D</span>
              <span>30D</span>
              <span>60日相关</span>
              <span>β</span>
              <span>走势</span>
            </div>
            {(data.series ?? []).map((s: MacroSeries) => {
              const c = byKey.get(s.key)
              return (
                <div className="macro-row" key={s.key}>
                  <span className="macro-name">{s.name}</span>
                  <span>{s.last != null ? s.last.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '--'}</span>
                  <span className={(s.chg1d ?? 0) >= 0 ? 'text-up' : 'text-down'}>{formatPct(s.chg1d)}</span>
                  <span className={(s.chg30d ?? 0) >= 0 ? 'text-up' : 'text-down'}>{formatPct(s.chg30d)}</span>
                  <span className={corrColor(c?.corr60 ?? null)}>
                    {c?.corr60 != null ? c.corr60.toFixed(2) : '--'}
                  </span>
                  <span>{c?.beta60 != null ? c.beta60.toFixed(2) : '--'}</span>
                  <span className="macro-spark-cell">
                    <Spark points={s.spark} />
                  </span>
                </div>
              )
            })}
          </div>
          <div className="pos-note">{data.note}</div>
        </>
      )}
    </div>
  )
}

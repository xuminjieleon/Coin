import { useCallback, useEffect, useState } from 'react'
import { advisePortfolio, type PortfolioAdvice } from '../api/client'
import { formatUsd, formatPrice } from '../utils/format'
import SourceHint from './SourceHint'

interface SavedPosition {
  direction: 'long' | 'short'
  entry: string
  stop: string
  qty: string
  leverage: string
  openedAt: string
}

const STORAGE_KEY = 'coinlens.position'
const EQUITY_KEY = 'coinlens.equity'

const LEVEL_CLASS: Record<string, string> = {
  ok: 'pos-item-ok',
  info: 'pos-item-info',
  warn: 'pos-item-warn',
  danger: 'pos-item-danger',
}

function loadAll(): Record<string, SavedPosition> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as Record<string, SavedPosition>) : {}
  } catch {
    return {}
  }
}

function loadEquity(): string {
  try {
    return localStorage.getItem(EQUITY_KEY) ?? ''
  } catch {
    return ''
  }
}

interface Props {
  /** recompute on data refresh */
  refreshKey: number | null
}

export default function PortfolioPanel({ refreshKey }: Props) {
  const [advice, setAdvice] = useState<PortfolioAdvice | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [equity, setEquity] = useState<string>(loadEquity)
  const [count, setCount] = useState(0)
  const [busy, setBusy] = useState(false)

  const compute = useCallback(async () => {
    const all = loadAll()
    const symbols = Object.keys(all)
    setCount(symbols.length)
    if (symbols.length === 0) {
      setAdvice(null)
      return
    }
    const equityNum = Number(equity)
    setBusy(true)
    setErr(null)
    try {
      const res = await advisePortfolio({
        positions: symbols.map((sym) => {
          const p = all[sym]
          return {
            symbol: sym,
            interval: '4h',
            direction: p.direction,
            entry: Number(p.entry),
            stop: p.stop.trim() === '' ? null : Number(p.stop),
            qty: p.qty.trim() === '' ? null : Number(p.qty),
            leverage: p.leverage.trim() === '' ? null : Number(p.leverage),
            openedAt: p.openedAt ? new Date(p.openedAt).getTime() : null,
          }
        }),
        accountEquity: Number.isFinite(equityNum) && equityNum > 0 ? equityNum : null,
      })
      setAdvice(res)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [equity])

  useEffect(() => {
    void compute()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  if (count === 0) return null

  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">组合风控（{count} 个仓位）</div>
        <SourceHint
          text="汇总「我的仓位」里保存的全部标的位置：净/总敞口、保证金、止损风险预算、集中度、两两相关性（本地 1d K 线 90 日）与对 BTC 的 beta。机构风控在组合层：单笔都对、组合仍可能是一注同向的相关性赌注。"
        />
      </div>
      <div className="pf-equity-row">
        <input
          type="number"
          placeholder="账户权益 USDT（计算风险占比）"
          value={equity}
          onChange={(e) => setEquity(e.target.value)}
          onBlur={() => {
            try {
              localStorage.setItem(EQUITY_KEY, equity)
            } catch {
              /* ignore */
            }
            void compute()
          }}
        />
        <button type="button" onClick={() => void compute()} disabled={busy}>
          {busy ? '计算中…' : '重新计算'}
        </button>
      </div>
      {err && <div className="pos-error">{err}</div>}
      {advice && (
        <>
          <div className="pf-grid">
            <div className="deriv-item">
              <div className="deriv-label">净敞口</div>
              <div className={`deriv-value ${advice.netUsd >= 0 ? 'text-up' : 'text-down'}`}>
                {formatUsd(advice.netUsd)}
              </div>
              <div className="deriv-sub">{advice.netUsd >= 0 ? '净多' : '净空'}</div>
            </div>
            <div className="deriv-item">
              <div className="deriv-label">总敞口</div>
              <div className="deriv-value">{formatUsd(advice.grossUsd)}</div>
              <div className="deriv-sub">
                {advice.marginUsd != null ? `保证金 ${formatUsd(advice.marginUsd)}` : '--'}
              </div>
            </div>
            <div className="deriv-item">
              <div className="deriv-label">止损风险合计</div>
              <div className="deriv-value">{formatUsd(advice.totalRiskUsd)}</div>
              <div className="deriv-sub">
                {advice.riskPctOfEquity != null ? `占权益 ${advice.riskPctOfEquity}%` : '填权益算占比'}
              </div>
            </div>
          </div>
          <div className="pf-positions">
            {advice.positions
              .filter((p) => p.notionalUsd != null)
              .map((p) => (
                <div className="pf-pos-row" key={`${p.symbol}-${p.interval}`}>
                  <span className={p.direction === 'long' ? 'text-up' : 'text-down'}>
                    {p.direction === 'long' ? '多' : '空'} {p.symbol.replace(/USDT$/, '')}
                  </span>
                  <span>{formatUsd(p.notionalUsd)}</span>
                  <span className={(p.unrealizedPct ?? 0) >= 0 ? 'text-up' : 'text-down'}>
                    {(p.unrealizedPct ?? 0) >= 0 ? '+' : ''}
                    {p.unrealizedPct?.toFixed(2)}%
                  </span>
                  {p.liqPrice != null && <span className="text-dim">强平≈{formatPrice(p.liqPrice)}</span>}
                </div>
              ))}
          </div>
          <ul className="pos-items">
            {advice.items.map((it, i) => (
              <li key={i} className={LEVEL_CLASS[it.level] ?? 'pos-item-info'}>
                {it.text}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

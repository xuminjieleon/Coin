import { useCallback, useEffect, useState } from 'react'
import { fetchScan, type ScanResponse } from '../api/client'
import type { Interval } from '../types'

interface Props {
  open: boolean
  interval: Interval
  currentSymbol: string
  onSelect: (symbol: string) => void
  onClose: () => void
}

const BIAS_CLASS: Record<string, string> = {
  bullish: 'scan-bull',
  bearish: 'scan-bear',
  neutral: 'scan-neutral',
}

export default function ScannerModal({ open, interval, currentSymbol, onSelect, onClose }: Props) {
  const [data, setData] = useState<ScanResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const res = await fetchScan(interval, 40)
      setData(res)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [interval])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    if (open) window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const rows = (data?.rows ?? []).filter((r) => !filter || r.symbol.toLowerCase().includes(filter.toLowerCase()))
  const bull = rows.filter((r) => r.bias === 'bullish').length
  const bear = rows.filter((r) => r.bias === 'bearish').length

  return (
    <div className="scan-overlay" onClick={onClose}>
      <div className="scan-modal" onClick={(e) => e.stopPropagation()}>
        <div className="scan-header">
          <div className="scan-title">
            全市场扫描 <span className="scan-sub">{interval} · 前 {data?.scanned ?? 40} 流动性</span>
          </div>
          <input
            className="scan-filter"
            placeholder="过滤符号…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <button className="scan-refresh" onClick={() => void load()} disabled={loading} type="button">
            {loading ? '扫描中…' : '重新扫描'}
          </button>
          <button className="scan-close" onClick={onClose} type="button">
            ✕
          </button>
        </div>
        {data && (
          <div className="scan-breadth">
            看多 <b className="text-up">{bull}</b> / 看空 <b className="text-down">{bear}</b> / 中性{' '}
            {rows.length - bull - bear}
            <span className="scan-updated">
              更新 {new Date(data.updatedAt).toLocaleTimeString('zh-CN')} · {data.durationMs}ms
            </span>
          </div>
        )}
        <div className="scan-table-wrap">
          {err ? (
            <div className="panel-empty scan-error">扫描失败：{err}</div>
          ) : !data ? (
            <div className="panel-empty">首次扫描约需 10-20 秒（40 个标的 × 引擎评分），之后走缓存…</div>
          ) : (
            <table className="scan-table">
              <thead>
                <tr>
                  <th>标的</th>
                  <th>价格</th>
                  <th>24h</th>
                  <th>评分</th>
                  <th>方向</th>
                  <th>市况</th>
                  <th>CVD背离</th>
                  <th>计划</th>
                  <th>主因</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={r.symbol}
                    className={`scan-row ${r.symbol === currentSymbol ? 'scan-row-current' : ''}`}
                    onClick={() => {
                      onSelect(r.symbol)
                      onClose()
                    }}
                  >
                    <td className="scan-symbol">
                      {r.symbol.replace(/USDT$/, '')}
                      {r.symbol === currentSymbol && <span className="scan-current-tag">当前</span>}
                    </td>
                    <td>{r.last >= 1 ? r.last.toFixed(2) : r.last.toPrecision(4)}</td>
                    <td className={r.chg24h >= 0 ? 'text-up' : 'text-down'}>
                      {r.chg24h >= 0 ? '+' : ''}
                      {r.chg24h.toFixed(1)}%
                    </td>
                    <td className={r.score >= 0 ? 'text-up' : 'text-down'}>
                      {r.score > 0 ? '+' : ''}
                      {r.score}
                    </td>
                    <td>
                      <span className={`scan-bias ${BIAS_CLASS[r.bias]}`}>
                        {r.bias === 'bullish' ? '多' : r.bias === 'bearish' ? '空' : '中'}
                      </span>
                    </td>
                    <td>{r.regime === 'trending' ? '趋势' : '震荡'}</td>
                    <td className={r.cvdDiv === 'bullish' ? 'text-up' : r.cvdDiv === 'bearish' ? 'text-down' : ''}>
                      {r.cvdDiv === 'bullish' ? '看涨' : r.cvdDiv === 'bearish' ? '看跌' : '--'}
                    </td>
                    <td>{r.hasPlan ? '✓' : ''}</td>
                    <td className="scan-reason">{r.topReason ?? '--'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="scan-note">{data?.note}</div>
      </div>
    </div>
  )
}

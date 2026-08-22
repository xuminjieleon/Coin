import { useCallback, useEffect, useRef, useState } from 'react'
import { advisePosition, type PositionAdvice } from '../api/client'
import { formatPrice } from '../utils/format'
import SourceHint from './SourceHint'
import type { Interval } from '../types'

interface Props {
  symbol: string
  interval: Interval
  /** latest analysis timestamp — refreshes auto re-advise when a saved position exists */
  analysisTime: number | null
}

const STORAGE_KEY = 'coinlens.position'

interface SavedPosition {
  direction: 'long' | 'short'
  entry: string
  stop: string
  qty: string
  leverage: string // '' or >= 1
  openedAt: string // datetime-local value ('' if none)
}

function loadSaved(symbol: string): SavedPosition | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const all = JSON.parse(raw) as Record<string, SavedPosition>
    const p = all[symbol]
    return p ?? null
  } catch {
    return null
  }
}

function savePosition(symbol: string, p: SavedPosition | null): void {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const all = raw ? (JSON.parse(raw) as Record<string, SavedPosition>) : {}
    if (p) all[symbol] = p
    else delete all[symbol]
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all))
  } catch {
    /* ignore */
  }
}

function toMs(dtLocal: string): number | null {
  if (!dtLocal) return null
  const ms = new Date(dtLocal).getTime()
  return Number.isFinite(ms) ? ms : null
}

const LEVEL_CLASS: Record<string, string> = {
  ok: 'pos-item-ok',
  info: 'pos-item-info',
  warn: 'pos-item-warn',
  danger: 'pos-item-danger',
}

export default function PositionPanel({ symbol, interval, analysisTime }: Props) {
  const [saved, setSaved] = useState<SavedPosition | null>(() => loadSaved(symbol))
  const [direction, setDirection] = useState<'long' | 'short'>(saved?.direction ?? 'long')
  const [entry, setEntry] = useState(saved?.entry ?? '')
  const [stop, setStop] = useState(saved?.stop ?? '')
  const [qty, setQty] = useState(saved?.qty ?? '')
  const [leverage, setLeverage] = useState(saved?.leverage ?? '1')
  const [openedAt, setOpenedAt] = useState(saved?.openedAt ?? '')
  const [advice, setAdvice] = useState<PositionAdvice | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const autoRunRef = useRef(false)

  // symbol switch: restore saved inputs for the new symbol
  useEffect(() => {
    const p = loadSaved(symbol)
    setSaved(p)
    setDirection(p?.direction ?? 'long')
    setEntry(p?.entry ?? '')
    setStop(p?.stop ?? '')
    setQty(p?.qty ?? '')
    setLeverage(p?.leverage ?? '1')
    setOpenedAt(p?.openedAt ?? '')
    setAdvice(null)
    setErr(null)
    autoRunRef.current = false
  }, [symbol])

  const runAdvise = useCallback(
    async (input: SavedPosition): Promise<void> => {
      const entryNum = Number(input.entry)
      if (!Number.isFinite(entryNum) || entryNum <= 0) {
        setErr('请输入有效的入场价')
        return
      }
      const stopNum = input.stop.trim() === '' ? null : Number(input.stop)
      if (stopNum != null && !Number.isFinite(stopNum)) {
        setErr('止损价无效')
        return
      }
      const qtyNum = input.qty.trim() === '' ? null : Number(input.qty)
      if (qtyNum != null && (!Number.isFinite(qtyNum) || qtyNum <= 0)) {
        setErr('数量无效')
        return
      }
      const levNum = input.leverage.trim() === '' ? null : Number(input.leverage)
      if (levNum != null && (!Number.isFinite(levNum) || levNum < 1)) {
        setErr('杠杆无效（需 ≥ 1）')
        return
      }
      setBusy(true)
      setErr(null)
      try {
        const res = await advisePosition({
          symbol,
          interval,
          direction: input.direction,
          entry: entryNum,
          stop: stopNum,
          qty: qtyNum,
          leverage: levNum,
          openedAt: toMs(input.openedAt),
        })
        setAdvice(res)
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e))
      } finally {
        setBusy(false)
      }
    },
    [symbol, interval],
  )

  // auto re-advise on data refresh when a position was already analyzed here
  useEffect(() => {
    if (analysisTime == null || !saved) return
    if (!autoRunRef.current) return
    void runAdvise(saved)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysisTime])

  const submit = () => {
    const input: SavedPosition = { direction, entry, stop, qty, leverage, openedAt }
    savePosition(symbol, input)
    setSaved(input)
    autoRunRef.current = true
    void runAdvise(input)
  }

  const clear = () => {
    savePosition(symbol, null)
    setSaved(null)
    setAdvice(null)
    setErr(null)
    setEntry('')
    setStop('')
    setQty('')
    setLeverage('1')
    setOpenedAt('')
    autoRunRef.current = false
  }

  const hasInput = entry.trim() !== ''

  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">我的仓位</div>
        <SourceHint
          text={`根据当前 ${interval} 盘面与校准后的计划几何（止损倍数、+R 减仓位、跟踪止盈、时间退出窗口）对持仓给出规则化建议：顺势/逆势检查、止损建议、减仓与保本移损提醒、剩余仓位跟踪止盈位（需要填写开仓时间）、时间退出提醒、保证金与强平价提示（需要填写杠杆与数量）。建议为纪律提示而非预测，请自行评估风险。`}
        />
      </div>
      <div className="pos-form">
        <div className="pos-dir-row">
          <button
            className={`pos-dir-btn ${direction === 'long' ? 'pos-dir-long' : ''}`}
            onClick={() => setDirection('long')}
            type="button"
          >
            做多
          </button>
          <button
            className={`pos-dir-btn ${direction === 'short' ? 'pos-dir-short' : ''}`}
            onClick={() => setDirection('short')}
            type="button"
          >
            做空
          </button>
        </div>
        <div className="pos-grid">
          <label className="pos-field">
            <span>入场价 *</span>
            <input
              type="number"
              value={entry}
              onChange={(e) => setEntry(e.target.value)}
              placeholder="0.00"
              step="any"
            />
          </label>
          <label className="pos-field">
            <span>止损价</span>
            <input
              type="number"
              value={stop}
              onChange={(e) => setStop(e.target.value)}
              placeholder="未设（将建议）"
              step="any"
            />
          </label>
          <label className="pos-field">
            <span>数量</span>
            <input
              type="number"
              value={qty}
              onChange={(e) => setQty(e.target.value)}
              placeholder="可选"
              step="any"
            />
          </label>
          <label className="pos-field">
            <span>杠杆（倍）</span>
            <input
              type="number"
              value={leverage}
              onChange={(e) => setLeverage(e.target.value)}
              placeholder="1"
              min="1"
              step="any"
            />
          </label>
          <label className="pos-field">
            <span>开仓时间</span>
            <input
              type="datetime-local"
              value={openedAt}
              onChange={(e) => setOpenedAt(e.target.value)}
            />
          </label>
        </div>
        <div className="pos-actions">
          <button className="pos-run-btn" onClick={submit} disabled={busy || !hasInput} type="button">
            {busy ? '分析中…' : '分析仓位'}
          </button>
          {saved && (
            <button className="pos-clear-btn" onClick={clear} type="button">
              清除
            </button>
          )}
        </div>
      </div>
      {err && <div className="pos-error">{err}</div>}
      {advice && (
        <div className="pos-result">
          <div className="pos-headline">
            <span>
              现价 <b>{formatPrice(advice.price)}</b>
            </span>
            <span className={advice.pnlPct >= 0 ? 'pos-pnl-up' : 'pos-pnl-down'}>
              {advice.pnlPct >= 0 ? '+' : ''}
              {advice.pnlPct.toFixed(2)}% · {advice.unrealizedR >= 0 ? '+' : ''}
              {advice.unrealizedR.toFixed(2)}R
            </span>
            {advice.barsHeld != null && <span>持仓 {advice.barsHeld} 根</span>}
            {advice.levels.liqPrice != null && (
              <span className="pos-pnl-down" title="隔离保证金近似强平价（未计维持保证金）">
                强平 ≈ {formatPrice(advice.levels.liqPrice)}
              </span>
            )}
          </div>
          <ul className="pos-items">
            {advice.items.map((it, i) => (
              <li key={i} className={LEVEL_CLASS[it.level] ?? 'pos-item-info'}>
                {it.text}
              </li>
            ))}
          </ul>
          <div className="pos-note">{advice.note}</div>
        </div>
      )}
    </div>
  )
}

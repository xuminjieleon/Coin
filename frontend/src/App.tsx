import { useCallback, useEffect, useRef, useState } from 'react'
import Header from './components/Header'
import ChartPanel, { type ChartPanelHandle } from './components/ChartPanel'
import MtfBar from './components/MtfBar'
import DecisionCard from './components/DecisionCard'
import TradePlanCard from './components/TradePlanCard'
import DerivativesPanel from './components/DerivativesPanel'
import VolumeProfilePanel from './components/VolumeProfilePanel'
import CalendarPanel from './components/CalendarPanel'
import {
  fetchAnalysis,
  fetchBacktest,
  fetchDerivatives,
  type AnalysisResponse,
  type BacktestResult,
  type Derivatives,
} from './api/client'
import type { WsKline, WsStatus } from './ws/binanceWs'
import { AlertEngine, pushNotification, requestNotifyPermission, type AlertMessage } from './utils/alerts'
import { DEFAULT_INTERVAL, DEFAULT_SYMBOL, type Interval } from './types'

const STORAGE_KEY = 'coinlens.symbol'

function loadSymbol(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? DEFAULT_SYMBOL
  } catch {
    return DEFAULT_SYMBOL
  }
}

const TOAST_TTL_MS = 10_000

export default function App() {
  const [symbol, setSymbol] = useState<string>(loadSymbol)
  const [interval, setIntervalValue] = useState<Interval>(DEFAULT_INTERVAL)
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null)
  const [derivatives, setDerivatives] = useState<Derivatives | null>(null)
  const [backtest, setBacktest] = useState<BacktestResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [wsStatus, setWsStatus] = useState<WsStatus>('connecting')
  const [polling, setPolling] = useState(false)
  const [alertsEnabled, setAlertsEnabled] = useState(false)
  const [toasts, setToasts] = useState<AlertMessage[]>([])
  const chartRef = useRef<ChartPanelHandle>(null)
  const analysisTimerRef = useRef<number | undefined>(undefined)
  const symbolRef = useRef(symbol)
  const intervalRef = useRef(interval)
  const alertEngineRef = useRef<AlertEngine>(new AlertEngine())
  const toastTimersRef = useRef<number[]>([])
  symbolRef.current = symbol
  intervalRef.current = interval

  const dismissToast = useCallback((id: string) => {
    setToasts((ts) => ts.filter((t) => t.id !== id))
  }, [])

  const emitAlerts = useCallback(
    (messages: AlertMessage[]) => {
      if (messages.length === 0) return
      setToasts((ts) => [...ts, ...messages].slice(-6))
      for (const m of messages) {
        pushNotification('CoinLens 预警', m.text)
        const timer = window.setTimeout(() => dismissToast(m.id), TOAST_TTL_MS)
        toastTimersRef.current.push(timer)
      }
    },
    [dismissToast],
  )

  const handleAnalysis = useCallback(
    (a: AnalysisResponse) => {
      setAnalysis(a)
      setError(null)
      // feed alert engine with key levels + structure events
      const engine = alertEngineRef.current
      engine.setLevels(
        a.summary.keyLevels.map((k) => ({ price: k.price, label: k.label })),
      )
      emitAlerts(
        engine.checkEvents(a.smc.structureEvents, a.smc.sweepEvents, a.symbol),
      )
    },
    [emitAlerts],
  )

  const handleError = useCallback((msg: string) => {
    setError(msg)
  }, [])

  // Manual refresh (candle close) — chart data keeps flowing via WS subscribeBar
  const refreshAnalysis = useCallback(async (): Promise<AnalysisResponse | null> => {
    setLoading(true)
    try {
      const data = await fetchAnalysis(symbolRef.current, intervalRef.current, 500)
      if (data.symbol === symbolRef.current && data.interval === intervalRef.current) {
        handleAnalysis(data)
        return data
      }
      return null
    } catch (e) {
      setError(`分析数据刷新失败：${e instanceof Error ? e.message : String(e)}`)
      return null
    } finally {
      setLoading(false)
    }
  }, [handleAnalysis])

  // New candle -> debounced analysis refresh
  const handleWsKline = useCallback(
    (k: WsKline) => {
      if (!k.isNew) return
      // price-level alerts on every update
      const msgs = alertEngineRef.current.checkPrice(k.close, symbolRef.current)
      emitAlerts(msgs)
      if (analysisTimerRef.current !== undefined) window.clearTimeout(analysisTimerRef.current)
      analysisTimerRef.current = window.setTimeout(() => {
        analysisTimerRef.current = undefined
        refreshAnalysis()
      }, 3000)
    },
    [refreshAnalysis, emitAlerts],
  )

  const loadDerivatives = useCallback(async () => {
    try {
      const data = await fetchDerivatives(symbolRef.current)
      setDerivatives(data)
    } catch {
      setDerivatives(null)
    }
  }, [])

  const loadBacktest = useCallback(async () => {
    setBacktest(null)
    try {
      const data = await fetchBacktest(symbolRef.current, intervalRef.current)
      if (data.symbol === symbolRef.current && data.interval === intervalRef.current) {
        setBacktest(data)
      }
    } catch {
      /* backtest is optional */
    }
  }, [])

  // Reset panels + reload derivatives/backtest on symbol/interval change
  useEffect(() => {
    setAnalysis(null)
    setDerivatives(null)
    loadDerivatives()
    loadBacktest()
    try {
      localStorage.setItem(STORAGE_KEY, symbol)
    } catch {
      /* ignore */
    }
  }, [symbol, interval, loadDerivatives, loadBacktest])

  // Poll derivatives every 30s
  useEffect(() => {
    const t = window.setInterval(loadDerivatives, 30_000)
    return () => window.clearInterval(t)
  }, [loadDerivatives])

  // Polling fallback: when the WS is unreachable (blocked network), refresh
  // chart candles + analysis every 60s via REST. Push the last candle into the
  // chart in-place so the view does not reset.
  useEffect(() => {
    if (wsStatus !== 'closed') {
      setPolling(false)
      return
    }
    setPolling(true)
    const poll = async () => {
      const a = await refreshAnalysis()
      if (a && a.candles.length > 0) {
        chartRef.current?.pushBar(a.candles[a.candles.length - 1])
        const price = a.candles[a.candles.length - 1].close
        emitAlerts(alertEngineRef.current.checkPrice(price, a.symbol))
      }
    }
    void poll()
    const t = window.setInterval(poll, 60_000)
    return () => window.clearInterval(t)
  }, [wsStatus, refreshAnalysis, emitAlerts])

  // Toggle alerts (requests browser notification permission)
  const toggleAlerts = useCallback(async () => {
    const engine = alertEngineRef.current
    if (engine.isEnabled()) {
      engine.setEnabled(false)
      setAlertsEnabled(false)
      return
    }
    engine.setEnabled(true)
    setAlertsEnabled(true)
    await requestNotifyPermission()
  }, [])

  // Cleanup timers
  useEffect(() => {
    return () => {
      if (analysisTimerRef.current !== undefined) window.clearTimeout(analysisTimerRef.current)
      for (const t of toastTimersRef.current) window.clearTimeout(t)
    }
  }, [])

  const lastClose =
    analysis && analysis.candles.length > 0
      ? analysis.candles[analysis.candles.length - 1].close
      : null

  return (
    <div className="app">
      <Header
        symbol={symbol}
        interval={interval}
        wsStatus={wsStatus}
        polling={polling}
        alertsEnabled={alertsEnabled}
        onToggleAlerts={toggleAlerts}
        onSymbol={setSymbol}
        onInterval={setIntervalValue}
      />
      {error && <div className="error-banner">{error}</div>}
      <MtfBar mtf={analysis?.mtf ?? null} summary={analysis?.summary ?? null} interval={interval} />
      <div className="main">
        <div className="chart-area">
          {loading && !analysis && <div className="loading-overlay">分析加载中…</div>}
          <ChartPanel
            ref={chartRef}
            symbol={symbol}
            interval={interval}
            analysis={analysis}
            onAnalysis={handleAnalysis}
            onWsKline={handleWsKline}
            onWsStatus={setWsStatus}
            onError={handleError}
          />
        </div>
        <aside className="sidebar">
          <DecisionCard analysis={analysis} backtest={backtest} />
          <TradePlanCard plan={analysis?.summary.tradePlan ?? null} />
          <DerivativesPanel derivatives={derivatives} symbol={symbol} />
          <VolumeProfilePanel
            profile={analysis?.volumeProfile ?? null}
            currentPrice={lastClose}
            symbol={symbol}
          />
          <CalendarPanel />
        </aside>
      </div>
      <div className="toast-stack">
        {toasts.map((t) => (
          <div className="toast" key={t.id} onClick={() => dismissToast(t.id)}>
            <span className="toast-icon">🔔</span>
            <span className="toast-text">{t.text}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

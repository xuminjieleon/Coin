import { useCallback, useEffect, useRef, useState } from 'react'
import Header from './components/Header'
import ChartPanel, { type ChartPanelHandle } from './components/ChartPanel'
import MtfBar from './components/MtfBar'
import DecisionCard from './components/DecisionCard'
import TradePlanCard from './components/TradePlanCard'
import PositionPanel from './components/PositionPanel'
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
import { AlertEngine, pushNotification, requestNotifyPermission, type AlertMessage } from './utils/alerts'
import { DEFAULT_INTERVAL, DEFAULT_SYMBOL, type Interval } from './types'

const STORAGE_KEY = 'coinlens.symbol'
const AUTO_REFRESH_KEY = 'coinlens.autoRefresh'
const AUTO_REFRESH_MS = 5 * 60 * 1000

function loadSymbol(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? DEFAULT_SYMBOL
  } catch {
    return DEFAULT_SYMBOL
  }
}

function loadAutoRefresh(): boolean {
  try {
    return localStorage.getItem(AUTO_REFRESH_KEY) !== 'off'
  } catch {
    return true
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
  const [refreshing, setRefreshing] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState<boolean>(loadAutoRefresh)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)
  const [alertsEnabled, setAlertsEnabled] = useState(false)
  const [toasts, setToasts] = useState<AlertMessage[]>([])
  const chartRef = useRef<ChartPanelHandle>(null)
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
      setLastRefresh(new Date())
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

  // Unified refresh (manual button + 5-min auto): analysis, chart tail,
  // derivatives and backtest in one pass. No real-time push.
  const refreshData = useCallback(async () => {
    setRefreshing(true)
    try {
      const data = await fetchAnalysis(symbolRef.current, intervalRef.current, 500)
      if (data.symbol === symbolRef.current && data.interval === intervalRef.current) {
        handleAnalysis(data)
        chartRef.current?.syncBars(data.candles)
        if (data.candles.length > 0) {
          const price = data.candles[data.candles.length - 1].close
          emitAlerts(alertEngineRef.current.checkPrice(price, data.symbol))
        }
      }
    } catch (e) {
      setError(`数据刷新失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setRefreshing(false)
    }
    void loadDerivatives()
    void loadBacktest()
  }, [handleAnalysis, loadDerivatives, loadBacktest, emitAlerts])

  // Auto refresh every 5 minutes (timer restarts on symbol/interval change)
  useEffect(() => {
    if (!autoRefresh) return
    const t = window.setInterval(() => void refreshData(), AUTO_REFRESH_MS)
    return () => window.clearInterval(t)
  }, [autoRefresh, symbol, interval, refreshData])

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

  const toggleAutoRefresh = useCallback(() => {
    setAutoRefresh((prev) => {
      const next = !prev
      try {
        localStorage.setItem(AUTO_REFRESH_KEY, next ? 'on' : 'off')
      } catch {
        /* ignore */
      }
      return next
    })
  }, [])

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
        refreshing={refreshing}
        autoRefresh={autoRefresh}
        lastRefresh={lastRefresh}
        onRefresh={() => void refreshData()}
        onToggleAutoRefresh={toggleAutoRefresh}
        alertsEnabled={alertsEnabled}
        onToggleAlerts={toggleAlerts}
        onSymbol={setSymbol}
        onInterval={setIntervalValue}
      />
      {error && <div className="error-banner">{error}</div>}
      <MtfBar mtf={analysis?.mtf ?? null} summary={analysis?.summary ?? null} interval={interval} />
      <div className="main">
        <div className="chart-area">
          {refreshing && !analysis && <div className="loading-overlay">分析加载中…</div>}
          <ChartPanel
            ref={chartRef}
            symbol={symbol}
            interval={interval}
            analysis={analysis}
            onAnalysis={handleAnalysis}
            onError={handleError}
          />
        </div>
        <aside className="sidebar">
          <DecisionCard analysis={analysis} backtest={backtest} />
          <TradePlanCard plan={analysis?.summary.tradePlan ?? null} interval={interval} />
          <PositionPanel
            symbol={symbol}
            interval={interval}
            analysisTime={analysis?.candles?.length ? analysis.candles[analysis.candles.length - 1].time : null}
          />
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

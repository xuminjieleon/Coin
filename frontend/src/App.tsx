import { useCallback, useEffect, useRef, useState } from 'react'
import Header from './components/Header'
import ChartPanel, { type ChartPanelHandle } from './components/ChartPanel'
import MtfBar from './components/MtfBar'
import DecisionCard from './components/DecisionCard'
import TradePlanCard from './components/TradePlanCard'
import PositionPanel from './components/PositionPanel'
import PortfolioPanel from './components/PortfolioPanel'
import JournalPanel from './components/JournalPanel'
import DerivativesPanel from './components/DerivativesPanel'
import VolumeProfilePanel from './components/VolumeProfilePanel'
import CalendarPanel from './components/CalendarPanel'
import OrderBookPanel from './components/OrderBookPanel'
import LiquidationPanel from './components/LiquidationPanel'
import OnchainPanel from './components/OnchainPanel'
import MacroPanel from './components/MacroPanel'
import ScannerModal from './components/ScannerModal'
import {
  fetchAnalysis,
  fetchBacktest,
  fetchDerivatives,
  fetchLiquidations,
  fetchMacro,
  fetchOnchain,
  fetchOrderbook,
  type AnalysisResponse,
  type BacktestResult,
  type Derivatives,
  type Liquidations,
  type MacroResponse,
  type OnchainResponse,
  type OrderBook,
} from './api/client'
import { AlertEngine, pushNotification, requestNotifyPermission, type AlertMessage } from './utils/alerts'
import { DEFAULT_INTERVAL, DEFAULT_SYMBOL, type Interval } from './types'

const STORAGE_KEY = 'coinlens.symbol'
const AUTO_REFRESH_KEY = 'coinlens.autoRefresh'
const TAB_KEY = 'coinlens.tab'
const AUTO_REFRESH_MS = 5 * 60 * 1000

type SidebarTab = 'decision' | 'market' | 'trading'

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

function loadTab(): SidebarTab {
  try {
    const t = localStorage.getItem(TAB_KEY)
    if (t === 'market' || t === 'trading') return t
  } catch {
    /* ignore */
  }
  return 'decision'
}

const TOAST_TTL_MS = 10_000

export default function App() {
  const [symbol, setSymbol] = useState<string>(loadSymbol)
  const [interval, setIntervalValue] = useState<Interval>(DEFAULT_INTERVAL)
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null)
  const [derivatives, setDerivatives] = useState<Derivatives | null>(null)
  const [backtest, setBacktest] = useState<BacktestResult | null>(null)
  const [orderbook, setOrderbook] = useState<OrderBook | null>(null)
  const [liquidations, setLiquidations] = useState<Liquidations | null>(null)
  const [onchain, setOnchain] = useState<OnchainResponse | null>(null)
  const [macro, setMacro] = useState<MacroResponse | null>(null)
  const [tab, setTab] = useState<SidebarTab>(loadTab)
  const [scanOpen, setScanOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState<boolean>(loadAutoRefresh)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)
  const [alertsEnabled, setAlertsEnabled] = useState(false)
  const [toasts, setToasts] = useState<AlertMessage[]>([])
  // decision replay: click a candle -> analysis as of that candle
  const [replay, setReplay] = useState<{ time: number; analysis: AnalysisResponse | null; loading: boolean } | null>(null)
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

  const loadMarketPanels = useCallback(async () => {
    try {
      const ob = await fetchOrderbook(symbolRef.current)
      setOrderbook(ob)
    } catch {
      setOrderbook(null)
    }
    try {
      const liq = await fetchLiquidations(symbolRef.current)
      setLiquidations(liq)
    } catch {
      setLiquidations(null)
    }
  }, [])

  // global panels (server-cached, cheap): load once + on each refresh pass
  const loadGlobalPanels = useCallback(async () => {
    try {
      setOnchain(await fetchOnchain())
    } catch {
      /* keep previous */
    }
    try {
      setMacro(await fetchMacro())
    } catch {
      /* keep previous */
    }
  }, [])

  // Unified refresh (manual button + 5-min auto): analysis, chart tail,
  // derivatives, backtest, order book, liquidations in one pass.
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
    void loadMarketPanels()
    void loadGlobalPanels()
  }, [handleAnalysis, loadDerivatives, loadBacktest, loadMarketPanels, loadGlobalPanels, emitAlerts])

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
    setOrderbook(null)
    setLiquidations(null)
    setReplay(null)
    loadDerivatives()
    loadBacktest()
    loadMarketPanels()
    try {
      localStorage.setItem(STORAGE_KEY, symbol)
    } catch {
      /* ignore */
    }
  }, [symbol, interval, loadDerivatives, loadBacktest, loadMarketPanels])

  // global panels on mount
  useEffect(() => {
    void loadGlobalPanels()
  }, [loadGlobalPanels])

  const switchTab = useCallback((t: SidebarTab) => {
    setTab(t)
    try {
      localStorage.setItem(TAB_KEY, t)
    } catch {
      /* ignore */
    }
  }, [])

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

  // Candle click -> replay decision at that candle (fetch analysis asOf)
  const selectCandle = useCallback(
    (time: number) => {
      setTab('decision')
      setReplay((prev) => {
        if (prev && prev.time === time && prev.analysis) return prev
        return { time, analysis: null, loading: true }
      })
      void (async () => {
        try {
          const a = await fetchAnalysis(symbolRef.current, intervalRef.current, 500, time)
          setReplay((prev) => (prev && prev.time === time ? { time, analysis: a, loading: false } : prev))
        } catch (e) {
          setError(`回放分析失败：${e instanceof Error ? e.message : String(e)}`)
          setReplay((prev) => (prev && prev.time === time ? { ...prev, loading: false } : prev))
        }
      })()
    },
    [],
  )

  const exitReplay = useCallback(() => setReplay(null), [])

  const lastTime =
    analysis && analysis.candles.length > 0
      ? analysis.candles[analysis.candles.length - 1].time
      : null
  // decision-tab panels use the replay analysis when active
  const decisionAnalysis = replay?.analysis ?? analysis
  const decisionLastClose =
    decisionAnalysis && decisionAnalysis.candles.length > 0
      ? decisionAnalysis.candles[decisionAnalysis.candles.length - 1].close
      : null
  const replayDate = replay ? new Date(replay.time) : null
  const replayLabel = replayDate
    ? `${replayDate.getFullYear()}-${String(replayDate.getMonth() + 1).padStart(2, '0')}-${String(
        replayDate.getDate(),
      ).padStart(2, '0')} ${String(replayDate.getHours()).padStart(2, '0')}:${String(
        replayDate.getMinutes(),
      ).padStart(2, '0')}`
    : ''

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
        onScan={() => setScanOpen(true)}
      />
      {error && <div className="error-banner">{error}</div>}
      <MtfBar
        mtf={decisionAnalysis?.mtf ?? null}
        summary={decisionAnalysis?.summary ?? null}
        interval={interval}
      />
      <div className="main">
        <div className="chart-area">
          {refreshing && !analysis && <div className="loading-overlay">分析加载中…</div>}
          <ChartPanel
            ref={chartRef}
            symbol={symbol}
            interval={interval}
            analysis={decisionAnalysis}
            onAnalysis={handleAnalysis}
            onError={handleError}
            onCandleClick={selectCandle}
            replayTime={replay?.time ?? null}
          />
        </div>
        <aside className="sidebar">
          <div className="sidebar-tabs">
            <button
              className={`sidebar-tab ${tab === 'decision' ? 'active' : ''}`}
              onClick={() => switchTab('decision')}
              type="button"
            >
              决策
            </button>
            <button
              className={`sidebar-tab ${tab === 'market' ? 'active' : ''}`}
              onClick={() => switchTab('market')}
              type="button"
            >
              市场数据
            </button>
            <button
              className={`sidebar-tab ${tab === 'trading' ? 'active' : ''}`}
              onClick={() => switchTab('trading')}
              type="button"
            >
              交易
            </button>
          </div>
          {tab === 'decision' && (
            <>
              {replay && (
                <div className="replay-banner">
                  <span className="replay-banner-icon">⏱</span>
                  <span className="replay-banner-text">
                    回放 {replayLabel}
                    {replay.loading ? ' · 分析中…' : ''}
                  </span>
                  <button className="replay-exit-btn" onClick={exitReplay} type="button">
                    返回实时
                  </button>
                </div>
              )}
              <DecisionCard analysis={decisionAnalysis} backtest={backtest} />
              <TradePlanCard
                plan={decisionAnalysis?.summary.tradePlan ?? null}
                interval={interval}
              />
              <DerivativesPanel derivatives={derivatives} symbol={symbol} />
              <VolumeProfilePanel
                profile={decisionAnalysis?.volumeProfile ?? null}
                currentPrice={decisionLastClose}
                symbol={symbol}
              />
            </>
          )}
          {tab === 'market' && (
            <>
              <MacroPanel data={macro} />
              <OnchainPanel data={onchain} />
              <OrderBookPanel data={orderbook} symbol={symbol} />
              <LiquidationPanel data={liquidations} symbol={symbol} />
              <CalendarPanel />
            </>
          )}
          {tab === 'trading' && (
            <>
              <PortfolioPanel refreshKey={lastTime} />
              <PositionPanel
                symbol={symbol}
                interval={interval}
                analysisTime={lastTime}
              />
              <JournalPanel
                symbol={symbol}
                interval={interval}
                plan={analysis?.summary.tradePlan ?? null}
                refreshKey={lastTime}
              />
            </>
          )}
        </aside>
      </div>
      <ScannerModal
        open={scanOpen}
        interval={interval}
        currentSymbol={symbol}
        onSelect={setSymbol}
        onClose={() => setScanOpen(false)}
      />
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

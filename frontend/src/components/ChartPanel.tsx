import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import {
  init,
  dispose,
  registerOverlay,
  registerIndicator,
  getOverlayClass,
  type Chart,
  type KLineData,
  type Period,
  type OverlayCreate,
  type OverlayFigure,
  type OverlayCreateFiguresCallbackParams,
} from 'klinecharts'
import { fetchAnalysis, fetchKlines, type AnalysisResponse, type Candle } from '../api/client'
import { buildOverlaySpecs } from './smcOverlays'
import type { Interval } from '../types'

const CANDLE_PANE = 'candle_pane'
const VOL_PANE = 'vol_pane'
const RSI_PANE = 'rsi_pane'
const CVD_PANE = 'cvd_pane'
const OVERLAY_GROUP = 'smc'

const PERIOD_OF: Record<Interval, Period> = {
  '1h': { type: 'hour', span: 1 },
  '4h': { type: 'hour', span: 4 },
  '1d': { type: 'day', span: 1 },
  '1w': { type: 'week', span: 1 },
}

const REPLAY_GROUP = 'replay-mark'

function registerReplayMarkOverlay(): void {
  if (!getOverlayClass('replayMark')) {
    registerOverlay({
      name: 'replayMark',
      totalStep: 2,
      needDefaultPointFigure: false,
      needDefaultXAxisFigure: false,
      needDefaultYAxisFigure: false,
      createPointFigures: ({ coordinates, bounding }: OverlayCreateFiguresCallbackParams<never>): OverlayFigure[] => {
        if (coordinates.length < 1) return []
        const x = coordinates[0].x
        return [
          {
            key: 'band',
            type: 'rect',
            ignoreEvent: true,
            attrs: { x: x - 2, y: 0, width: 4, height: bounding.height },
            styles: {
              style: 'fill',
              backgroundColor: 'rgba(41, 98, 255, 0.35)',
              borderColor: 'rgba(41, 98, 255, 0.8)',
              borderSize: 1,
              borderStyle: 'solid',
            },
          },
        ]
      },
    })
  }
}

// ---------- custom overlays (registered once) ----------

interface RectExtend {
  color: string
  borderColor: string
  dashed: boolean
}

interface TextExtend {
  text: string
  color: string
  bold: boolean
}

interface PolylineExtend {
  color: string
}

function registerCustomOverlays(): void {
  if (!getOverlayClass('smcRect')) {
    registerOverlay({
      name: 'smcRect',
      totalStep: 3,
      needDefaultPointFigure: false,
      needDefaultXAxisFigure: false,
      needDefaultYAxisFigure: false,
      createPointFigures: ({
        overlay,
        coordinates,
        bounding,
      }: OverlayCreateFiguresCallbackParams<RectExtend>): OverlayFigure[] => {
        if (coordinates.length < 2) return []
        const left = Math.min(coordinates[0].x, coordinates[1].x)
        const top = Math.min(coordinates[0].y, coordinates[1].y)
        const height = Math.abs(coordinates[1].y - coordinates[0].y)
        const width = bounding.width - left
        if (width <= 0 || height <= 0) return []
        const ext = overlay.extendData
        return [
          {
            key: 'rect',
            type: 'rect',
            ignoreEvent: true,
            attrs: { x: left, y: top, width, height },
            styles: {
              style: 'stroke_fill',
              backgroundColor: ext?.color ?? 'rgba(255,255,255,0.08)',
              borderColor: ext?.borderColor ?? 'rgba(255,255,255,0.4)',
              borderSize: 1,
              borderStyle: ext?.dashed ? 'dashed' : 'solid',
              borderDashedValue: [4, 3],
            },
          },
        ]
      },
    })
  }
  if (!getOverlayClass('smcText')) {
    registerOverlay({
      name: 'smcText',
      totalStep: 2,
      needDefaultPointFigure: false,
      needDefaultXAxisFigure: false,
      needDefaultYAxisFigure: false,
      createPointFigures: ({
        overlay,
        coordinates,
      }: OverlayCreateFiguresCallbackParams<TextExtend>): OverlayFigure[] => {
        if (coordinates.length < 1) return []
        const ext = overlay.extendData
        return [
          {
            key: 'text',
            type: 'text',
            ignoreEvent: true,
            attrs: {
              x: coordinates[0].x,
              y: coordinates[0].y,
              text: ext?.text ?? '',
              align: 'center',
              baseline: 'middle',
            },
            styles: {
              color: ext?.color ?? '#9aa4b2',
              size: 10,
              weight: ext?.bold ? 'bold' : 'normal',
            },
          },
        ]
      },
    })
  }
  if (!getOverlayClass('smcPolyline')) {
    registerOverlay({
      name: 'smcPolyline',
      totalStep: 3,
      needDefaultPointFigure: false,
      needDefaultXAxisFigure: false,
      needDefaultYAxisFigure: false,
      createPointFigures: ({
        overlay,
        coordinates,
      }: OverlayCreateFiguresCallbackParams<PolylineExtend>): OverlayFigure[] => {
        if (coordinates.length < 2) return []
        const ext = overlay.extendData
        return [
          {
            key: 'line',
            type: 'line',
            ignoreEvent: true,
            attrs: {
              coordinates: coordinates.map((c) => ({ x: c.x, y: c.y })),
            },
            styles: {
              style: 'dashed',
              color: ext?.color ?? 'rgba(240,185,11,0.7)',
              size: 1,
              dashedValue: [4, 4],
            },
          },
        ]
      },
    })
  }
}

let cvdIndicatorRegistered = false

function registerCvdIndicator(): void {
  if (cvdIndicatorRegistered) return
  cvdIndicatorRegistered = true
  registerIndicator({
    name: 'CVD',
    shortName: 'CVD',
    figures: [{ key: 'cvd', title: 'CVD: ', type: 'line' }],
    calc: (dataList, indicator) => {
      // extendData: {time, cvd} pairs aligned by timestamp so prepended
      // history pages (backward paging) never shift the CVD line.
      const pairs = indicator.extendData as { time: number; cvd: number | null }[] | undefined
      if (!pairs) return dataList.map(() => ({ cvd: undefined }))
      const byTime = new Map(pairs.map((p) => [p.time, p.cvd]))
      return dataList.map((d) => ({ cvd: byTime.get(d.timestamp) ?? undefined }))
    },
  })
}

function toKLineData(c: Candle): KLineData {
  return { timestamp: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume }
}

function pricePrecisionOf(price: number): number {
  if (price < 0.01) return 6
  if (price < 1) return 4
  if (price < 100) return 3
  return 2
}

export interface ChartPanelHandle {
  /** Sync the tail of freshly fetched candles into the chart in place:
   *  bars newer than the chart's last bar are appended, the matching last
   *  bar is updated, older ones are ignored. View is never reset. */
  syncBars: (candles: Candle[]) => void
}

interface Props {
  symbol: string
  interval: Interval
  analysis: AnalysisResponse | null
  onAnalysis: (a: AnalysisResponse) => void
  onError: (msg: string) => void
  /** candle click (open timestamp) — enables decision replay */
  onCandleClick?: (time: number) => void
  /** replay anchor: draws a vertical marker band at this candle */
  replayTime?: number | null
}

const ChartPanel = forwardRef<ChartPanelHandle, Props>(function ChartPanel(
  { symbol, interval, analysis, onAnalysis, onError, onCandleClick, replayTime },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<Chart | null>(null)
  const symbolRef = useRef(symbol)
  const intervalRef = useRef(interval)
  const onAnalysisRef = useRef(onAnalysis)
  const onErrorRef = useRef(onError)
  const onCandleClickRef = useRef(onCandleClick)
  // last analysis fetch per data key (short-lived to avoid stale cache)
  const fetchSlotRef = useRef<{ key: string; at: number; promise: Promise<AnalysisResponse> } | null>(null)
  // bar push callback from the chart's data loader (subscribeBar)
  const barPushRef = useRef<((d: KLineData) => void) | null>(null)
  // history paging: 'idle' = more available, 'loading' = fetching a page, 'done' = no more
  const [historyState, setHistoryState] = useState<'idle' | 'loading' | 'done'>('idle')

  symbolRef.current = symbol
  intervalRef.current = interval
  onAnalysisRef.current = onAnalysis
  onErrorRef.current = onError
  onCandleClickRef.current = onCandleClick

  useImperativeHandle(
    ref,
    () => ({
      syncBars: (candles: Candle[]) => {
        const chart = chartRef.current
        if (!chart || candles.length === 0) return
        const list = chart.getDataList()
        const lastTs = list.length > 0 ? list[list.length - 1].timestamp : 0
        for (const c of candles) {
          if (c.time < lastTs) continue
          barPushRef.current?.(toKLineData(c))
        }
      },
    }),
    [],
  )

  registerCustomOverlays()
  registerCvdIndicator()
  registerReplayMarkOverlay()

  // Init chart + indicators + data loader once
  useEffect(() => {
    if (!containerRef.current) return
    const chart = init(containerRef.current, {
      locale: 'zh-CN',
      styles: {
        grid: {
          horizontal: { color: '#1c2128' },
          vertical: { color: '#1c2128' },
        },
        candle: {
          bar: {
            upColor: '#26a69a',
            downColor: '#ef5350',
            noChangeColor: '#6e7681',
            upBorderColor: '#26a69a',
            downBorderColor: '#ef5350',
            noChangeBorderColor: '#6e7681',
            upWickColor: '#26a69a',
            downWickColor: '#ef5350',
            noChangeWickColor: '#6e7681',
          },
        },
        xAxis: {
          axisLine: { color: '#21262d' },
          tickText: { color: '#76808f' },
          tickLine: { color: '#21262d' },
        },
        yAxis: {
          axisLine: { color: '#21262d' },
          tickText: { color: '#76808f' },
          tickLine: { color: '#21262d' },
        },
        separator: { color: '#21262d' },
        crosshair: {
          horizontal: { line: { color: '#4a5568' }, text: { backgroundColor: '#2962ff' } },
          vertical: { line: { color: '#4a5568' }, text: { backgroundColor: '#2962ff' } },
        },
      },
    })
    if (!chart) return
    chartRef.current = chart

    // EMA 20/50/200 stacked on candles
    chart.createIndicator(
      {
        name: 'EMA',
        calcParams: [20, 50, 200],
        shortName: 'EMA',
        paneId: CANDLE_PANE,
        styles: {
          lines: [
            { color: '#f0b90b', size: 1 },
            { color: '#ab7df8', size: 1 },
            { color: '#5d6673', size: 1 },
          ],
        },
      },
      true,
    )
    // Volume pane
    chart.createIndicator({ name: 'VOL', paneId: VOL_PANE })
    chart.setPaneOptions({ id: VOL_PANE, height: 70, minHeight: 40 })
    // RSI pane
    chart.createIndicator({
      name: 'RSI',
      calcParams: [14],
      paneId: RSI_PANE,
      minValue: 0,
      maxValue: 100,
      styles: { lines: [{ color: '#7e57c2', size: 1 }] },
    })
    chart.setPaneOptions({ id: RSI_PANE, height: 70, minHeight: 40 })
    // CVD pane (values injected per analysis via extendData)
    chart.createIndicator({
      name: 'CVD',
      paneId: CVD_PANE,
      styles: { lines: [{ color: '#4dd0e1', size: 1 }] },
    })
    chart.setPaneOptions({ id: CVD_PANE, height: 60, minHeight: 40 })

    // data loader: chart pulls candles on symbol/period change. Live updates are
    // NOT subscribed (manual / 5-min refresh mode); subscribeBar only keeps the
    // push callback so App can sync refreshed bars into the chart in place.
    // History paging: klinecharts fires type='forward' with the OLDEST loaded
    // timestamp when the view reaches the left edge (returned bars are
    // prepended); 'backward' would append newer bars, which refresh handles.
    chart.setDataLoader({
      getBars: async ({ type, timestamp, callback }) => {
        if (type === 'forward' && timestamp != null) {
          setHistoryState('loading')
          try {
            const page = await fetchKlines(symbolRef.current, intervalRef.current, 500, timestamp - 1)
            const bars = page.candles.map(toKLineData)
            const more = bars.length >= 500
            setHistoryState(more ? 'idle' : 'done')
            callback(bars, more ? { forward: true, backward: false } : false)
          } catch {
            setHistoryState('idle')
            callback([], false)
          }
          return
        }
        if (type !== 'init') {
          callback([], false)
          return
        }
        const sym = symbolRef.current
        const itv = intervalRef.current
        const key = `${sym}-${itv}`
        const stale =
          fetchSlotRef.current !== null && Date.now() - fetchSlotRef.current.at > 30_000
        if (!fetchSlotRef.current || fetchSlotRef.current.key !== key || stale) {
          fetchSlotRef.current = {
            key,
            at: Date.now(),
            promise: fetchAnalysis(sym, itv, 500).catch((e: unknown) => {
              fetchSlotRef.current = null
              throw e
            }),
          }
        }
        try {
          const a = await fetchSlotRef.current.promise
          onAnalysisRef.current(a)
          setHistoryState(a.candles.length >= 500 ? 'idle' : 'done')
          callback(a.candles.map(toKLineData), { forward: true, backward: false })
          // fix price precision now that magnitude is known
          const last = a.candles[a.candles.length - 1]
          const p = pricePrecisionOf(last.close)
          const cur = chartRef.current?.getSymbol()
          if (cur && cur.pricePrecision !== p) {
            chartRef.current?.setSymbol({ ticker: sym, pricePrecision: p, volumePrecision: 2 })
          }
        } catch (e) {
          fetchSlotRef.current = null
          onErrorRef.current(`分析数据加载失败：${e instanceof Error ? e.message : String(e)}`)
          callback([], false)
        }
      },
      subscribeBar: ({ callback }) => {
        barPushRef.current = callback
      },
      unsubscribeBar: () => {
        barPushRef.current = null
      },
    })

    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(containerRef.current)

    // candle click -> decision replay (payload: {dataIndex, data:{current}})
    const handleCandleClick = (data?: unknown) => {
      const d = data as { data?: { current?: { timestamp?: number } } } | undefined
      const ts = d?.data?.current?.timestamp
      if (typeof ts === 'number') onCandleClickRef.current?.(ts)
    }
    chart.subscribeAction('onCandleBarClick', handleCandleClick)

    return () => {
      ro.disconnect()
      chart.unsubscribeAction('onCandleBarClick', handleCandleClick)
      barPushRef.current = null
      dispose(containerRef.current!)
      chartRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // symbol / interval switch -> reload chart data via loader
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    setHistoryState('idle')
    chart.setPeriod(PERIOD_OF[interval])
    chart.setSymbol({ ticker: symbol })
  }, [symbol, interval])

  // "load more history" button: scroll to the oldest bar, which makes the
  // chart's data loader fire a forward (prepend) request for one more page.
  const loadMoreHistory = () => {
    chartRef.current?.scrollToDataIndex(0)
  }

  // replay marker: vertical band at the selected candle
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.removeOverlay({ groupId: REPLAY_GROUP })
    if (replayTime == null) return
    chart.createOverlay({
      name: 'replayMark',
      groupId: REPLAY_GROUP,
      points: [{ timestamp: replayTime }],
      lock: true,
      zLevel: 100,
    } as OverlayCreate)
  }, [replayTime])

  // SMC overlays from analysis + CVD indicator refresh
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.removeOverlay({ groupId: OVERLAY_GROUP })
    if (!analysis || analysis.candles.length === 0) return
    if (analysis.symbol !== symbol || analysis.interval !== interval) return
    const lastClose = analysis.candles[analysis.candles.length - 1].close

    // refresh CVD pane values (remove + recreate with new extendData)
    chart.removeIndicator({ name: 'CVD' })
    const cvdPairs = analysis.candles.map((c, i) => ({
      time: c.time,
      cvd: analysis.indicators.cvd?.[i] ?? null,
    }))
    chart.createIndicator({
      name: 'CVD',
      paneId: CVD_PANE,
      extendData: cvdPairs,
      styles: { lines: [{ color: '#4dd0e1', size: 1 }] },
    })

    const specs = buildOverlaySpecs(analysis.smc, lastClose, {
      pocSeries: analysis.volumeProfile.pocSeries,
      wyckoff: analysis.wyckoff,
    })
    for (const spec of specs) {
      if (spec.kind === 'rect') {
        const overlay: OverlayCreate = {
          name: 'smcRect',
          groupId: OVERLAY_GROUP,
          points: [
            { timestamp: spec.startTime, value: spec.top },
            { timestamp: spec.startTime, value: spec.bottom },
          ],
          extendData: { color: spec.color, borderColor: spec.borderColor, dashed: spec.dashed },
          lock: true,
        }
        chart.createOverlay(overlay)
      } else if (spec.kind === 'hline') {
        const overlay: OverlayCreate = {
          name: 'simpleTag',
          groupId: OVERLAY_GROUP,
          points: [{ value: spec.price }],
          extendData: spec.text ?? '',
          lock: true,
          styles: {
            line: {
              style: spec.dashed ? 'dashed' : 'solid',
              color: spec.color,
              size: 1,
            },
          },
        }
        chart.createOverlay(overlay)
      } else if (spec.kind === 'polyline') {
        if (spec.points.length < 2) continue
        const overlay: OverlayCreate = {
          name: 'smcPolyline',
          groupId: OVERLAY_GROUP,
          points: spec.points.map((p) => ({ timestamp: p.time, value: p.value })),
          extendData: { color: spec.color },
          lock: true,
        }
        chart.createOverlay(overlay)
      } else {
        const overlay: OverlayCreate = {
          name: 'smcText',
          groupId: OVERLAY_GROUP,
          points: [{ timestamp: spec.time, value: spec.price }],
          extendData: { text: spec.text, color: spec.color, bold: spec.bold },
          lock: true,
        }
        chart.createOverlay(overlay)
      }
    }
  }, [analysis, symbol, interval])

  return (
    <div className="chart-container" ref={containerRef}>
      {historyState !== 'done' && (
        <button
          className={`chart-history-btn${historyState === 'loading' ? ' chart-history-busy' : ''}`}
          onClick={loadMoreHistory}
          disabled={historyState === 'loading'}
          title="向左加载更早的历史 K 线（每次 500 根；也可直接把图表滚动到最左侧自动加载）"
        >
          {historyState === 'loading' ? '历史加载中…' : '⟵ 加载更多历史'}
        </button>
      )}
      <div className="chart-replay-hint" title="点击任意一根 K 线：以该 K 线及之前的全部数据（含衍生品/宏观因子时点值）重算当时的决策；再点其他 K 线可移动回放点">
        点击 K 线回放决策
      </div>
    </div>
  )
})

export default ChartPanel

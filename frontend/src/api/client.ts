const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

async function request<T>(path: string): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`)
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return (await resp.json()) as T
}

// ---- Types (match backend contract) ----

export interface SymbolInfo {
  symbol: string
  base: string
}

export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface SwingPoint {
  index: number
  time: number
  price: number
  kind: 'high' | 'low'
}

export interface StructureEvent {
  time: number
  price: number
  kind: 'BOS' | 'CHoCH'
  direction: 'bullish' | 'bearish'
}

export interface OrderBlock {
  top: number
  bottom: number
  startTime: number
  type: 'bullish' | 'bearish'
  mitigated: boolean
  quality?: number
}

export interface Fvg {
  top: number
  bottom: number
  startTime: number
  type: 'bullish' | 'bearish'
  mitigated: boolean
  quality?: number
}

export interface LiquidityPool {
  price: number
  type: 'buy_side' | 'sell_side'
  touches: number
  swept: boolean
}

export interface SweepEvent {
  time: number
  price: number
  side: 'buy_side' | 'sell_side'
  outcome: 'reclaimed' | 'broken'
  barsToResolve: number
}

export interface PremiumDiscount {
  rangeHigh: number
  rangeLow: number
  equilibrium: number
  position: 'premium' | 'discount' | 'equilibrium'
  pct: number
}

export interface SmcResult {
  swings: SwingPoint[]
  structureEvents: StructureEvent[]
  orderBlocks: OrderBlock[]
  fvgs: Fvg[]
  liquidityPools: LiquidityPool[]
  sweepEvents: SweepEvent[]
  premiumDiscount: PremiumDiscount
}

export interface Indicators {
  ema20: (number | null)[]
  ema50: (number | null)[]
  ema200: (number | null)[]
  rsi14: (number | null)[]
  atr14: (number | null)[]
  adx14: (number | null)[]
  cvd: (number | null)[]
}

export interface VolumeBin {
  priceLow: number
  priceHigh: number
  volume: number
}

export interface PocPoint {
  time: number
  poc: number
}

export interface VolumeProfile {
  poc: number
  vah: number
  val: number
  bins: VolumeBin[]
  developingPoc?: number
  pocSeries?: PocPoint[]
}

export interface KeyLevel {
  price: number
  label: string
}

export interface Reason {
  text: string
  direction: 'bullish' | 'bearish' | 'neutral'
  weight: number
}

export interface TradePlan {
  direction: 'long' | 'short'
  entry: number
  stop: number
  target1: number
  target2: number
  beTrigger?: number
  rr: number
  note: string
}

export interface Summary {
  score: number
  bias: 'bullish' | 'bearish' | 'neutral'
  regime: 'trending' | 'ranging'
  keyLevels: KeyLevel[]
  reasons: Reason[]
  tradePlan?: TradePlan | null
  highConfidence?: boolean
  cvdConfluence?: { direction: 'bullish' | 'bearish' | null; count: number } | null
}

export interface CandlePattern {
  time: number
  type: string
  direction: 'bullish' | 'bearish' | 'neutral'
  price: number
}

export interface ChartPattern {
  type: string
  direction: 'bullish' | 'bearish' | 'neutral'
  startTime: number
  endTime: number
  confidence: number
  keyLevel?: number | null
}

export interface Patterns {
  candles: CandlePattern[]
  charts: ChartPattern[]
}

export interface WyckoffEvent {
  time: number
  type: 'spring' | 'utad' | 'sos'
}

export interface Wyckoff {
  phase: 'accumulation' | 'distribution' | 'markup' | 'markdown' | 'none'
  events: WyckoffEvent[]
}

export interface Volatility {
  atrPct: number | null
  bandwidthPct: number | null
  squeeze: boolean
  state: 'compressed' | 'normal' | 'expanded'
}

export interface CvdDivergence {
  type: 'bullish' | 'bearish'
  strength: number
}

export interface MtfSummary {
  interval: string
  score: number
  bias: 'bullish' | 'bearish' | 'neutral'
  regime: 'trending' | 'ranging'
}

export interface Mtf {
  list: MtfSummary[]
  alignment: 'aligned' | 'mixed' | 'conflict' | 'none'
}

export interface AnalysisResponse {
  symbol: string
  interval: string
  candles: Candle[]
  smc: SmcResult
  indicators: Indicators
  volumeProfile: VolumeProfile
  patterns: Patterns
  wyckoff: Wyckoff
  volatility: Volatility
  cvdDivergence: CvdDivergence | null
  mtf: Mtf
  summary: Summary
}

export interface OiPoint {
  time: number
  value: number
}

export interface FundingPoint {
  time: number
  rate: number
}

export interface RatioPoint {
  time: number
  ratio: number
}

export interface OptionsSnapshot {
  atmIv: number | null
  putCallRatio: number | null
  contracts: number
  expiry: number
}

export interface Derivatives {
  openInterest: number | null
  openInterestValue: number | null
  oiChangePct24h: number | null
  oiHistory: OiPoint[] | null
  fundingRate: number | null
  fundingHistory: FundingPoint[] | null
  longShortRatio: number | null
  longShortHistory: RatioPoint[] | null
  takerBuySellRatio: number | null
  source?: 'binance' | 'gateio' | null
  options?: OptionsSnapshot | null
}

export interface BacktestResult {
  symbol: string
  interval: string
  horizon: number
  samples: number
  directionalSamples: number
  ic: number
  hitRate: number | null
  scoreSeries: { time: number; score: number }[]
}

export interface CalendarEvent {
  date: string
  time?: string
  title: string
  impact: string
  kind: string
}

export interface CalendarResponse {
  events: CalendarEvent[]
  note: string
}

// ---- API calls ----

export function fetchSymbols(q?: string): Promise<SymbolInfo[]> {
  const query = q ? `?q=${encodeURIComponent(q)}` : ''
  return request<SymbolInfo[]>(`/api/symbols${query}`)
}

export function fetchAnalysis(symbol: string, interval: string, limit = 500): Promise<AnalysisResponse> {
  return request<AnalysisResponse>(
    `/api/analysis?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=${limit}`,
  )
}

export function fetchDerivatives(symbol: string): Promise<Derivatives> {
  return request<Derivatives>(`/api/derivatives?symbol=${encodeURIComponent(symbol)}`)
}

export function fetchBacktest(symbol: string, interval: string): Promise<BacktestResult> {
  return request<BacktestResult>(
    `/api/backtest?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=600`,
  )
}

export function fetchCalendar(): Promise<CalendarResponse> {
  return request<CalendarResponse>('/api/calendar')
}

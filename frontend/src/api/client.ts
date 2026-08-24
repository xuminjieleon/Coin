const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

async function request<T>(path: string): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`)
  return handleResponse<T>(resp)
}

async function postRequest<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return handleResponse<T>(resp)
}

async function deleteRequest<T>(path: string): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, { method: 'DELETE' })
  return handleResponse<T>(resp)
}

async function handleResponse<T>(resp: Response): Promise<T> {
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
  target1: number | null
  target2?: number | null
  beTrigger?: number
  beR?: number
  targetR?: number | null
  scaleOut?: boolean
  trailR?: number | null
  stopAtr?: number
  depthAtr?: number
  texitBars?: number
  fillBars?: number
  rr: number | null
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
  wyckoff: Wyckoff
  volatility: Volatility
  cvdDivergence: CvdDivergence | null
  mtf: Mtf
  summary: Summary
  replay?: { asOf: number } | null
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
  rr25?: number | null
  maxPain?: { expiry: number; strike: number } | null
  termStructure?: { expiry: number; atmIv: number | null; rr25: number | null; putOi: number; callOi: number; pcr: number | null }[]
  contracts: number
  expiry: number
}

export interface HistoryStats {
  days: number
  fundingPctl: number | null
  oiUsdPctl: number | null
  lsrPctl: number | null
  liqDayPctlBase?: { median: number; p90: number | null; max: number; days: number } | null
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
  topTraderRatio?: number | null
  historyStats?: HistoryStats | null
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

export function fetchAnalysis(
  symbol: string,
  interval: string,
  limit = 500,
  asOf?: number,
): Promise<AnalysisResponse> {
  const suffix = asOf != null ? `&asOf=${asOf}` : ''
  return request<AnalysisResponse>(
    `/api/analysis?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=${limit}${suffix}`,
  )
}

export interface KlinesResponse {
  symbol: string
  interval: string
  candles: Candle[]
}

/** Raw klines for chart history paging (older bars before `endTime`). */
export function fetchKlines(
  symbol: string,
  interval: string,
  limit = 500,
  endTime?: number,
): Promise<KlinesResponse> {
  const end = endTime != null ? `&endTime=${endTime}` : ''
  return request<KlinesResponse>(
    `/api/klines?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=${limit}${end}`,
  )
}

export function fetchDerivatives(symbol: string): Promise<Derivatives> {
  return request<Derivatives>(`/api/derivatives?symbol=${encodeURIComponent(symbol)}`)
}

export function fetchBacktest(symbol: string, interval: string): Promise<BacktestResult> {
  return request<BacktestResult>(
    `/api/backtest?symbol=${encodeURIComponent(symbol)}&interval=${interval}`,
  )
}

export function fetchCalendar(): Promise<CalendarResponse> {
  return request<CalendarResponse>('/api/calendar')
}

// ---- Position advisor ----

export interface PositionAdviceItem {
  level: 'ok' | 'info' | 'warn' | 'danger'
  text: string
}

export interface PositionAdvice {
  symbol: string
  interval: string
  price: number
  pnlPct: number
  unrealizedR: number
  mfeR: number | null
  barsHeld: number | null
  levels: {
    suggestedStop: number
    beTrigger: number
    trailStop: number | null
    liqPrice: number | null
  }
  items: PositionAdviceItem[]
  note: string
}

export interface PositionInput {
  symbol: string
  interval: string
  direction: 'long' | 'short'
  entry: number
  stop?: number | null
  qty?: number | null
  leverage?: number | null
  openedAt?: number | null
}

export function advisePosition(input: PositionInput): Promise<PositionAdvice> {
  return postRequest<PositionAdvice>('/api/position/advise', input)
}

// ---- Order book / microstructure ----

export interface OrderBookBand {
  bandPct: number
  bidUsd: number
  askUsd: number
  imbalance: number | null
}

export interface OrderBookWall {
  side: 'bid' | 'ask'
  price: number
  usd: number
  distBps: number
}

export interface OrderBook {
  symbol: string
  source: string | null
  mid: number | null
  bestBid: number | null
  bestAsk: number | null
  spreadBps: number | null
  topImbalance: number | null
  bands: OrderBookBand[] | null
  walls: OrderBookWall[] | null
  levels: number
  note: string | null
}

export function fetchOrderbook(symbol: string): Promise<OrderBook> {
  return request<OrderBook>(`/api/orderbook?symbol=${encodeURIComponent(symbol)}`)
}

// ---- Liquidations ----

export interface LiquidationPoint {
  time: number
  longUsd: number
  shortUsd: number
}

export interface LiquidationEstimate {
  leverage: number
  longLiq: number
  shortLiq: number
}

export interface Liquidations {
  symbol: string
  long24hUsd: number
  short24hUsd: number
  total24hUsd: number
  longShortRatio: number | null
  percentileVsYear: number | null
  history: LiquidationPoint[]
  estimated: LiquidationEstimate[]
  price: number | null
  source: string | null
  note: string
}

export function fetchLiquidations(symbol: string): Promise<Liquidations> {
  return request<Liquidations>(`/api/liquidations?symbol=${encodeURIComponent(symbol)}`)
}

// ---- On-chain ----

export interface OnchainBtc {
  hashrate: number | null
  hashrateChg30d: number | null
  mempoolTxs: number | null
  mempoolVsize: number | null
  fees: { fastest: number | null; halfHour: number | null; hour: number | null; economy: number | null } | null
  difficulty: { progressPct: number | null; difficultyChangePct: number | null; remainingBlocks: number | null; estimatedRetargetDate: number | null } | null
  activeAddresses: number | null
  activeAddrAvg30d: number | null
  txCount24h: number | null
}

export interface OnchainResponse {
  btc: OnchainBtc
  sources: string[]
  unavailable: string
  updatedAt: number
}

export function fetchOnchain(): Promise<OnchainResponse> {
  return request<OnchainResponse>('/api/onchain')
}

// ---- Macro linkage ----

export interface MacroSeries {
  key: string
  name: string
  last: number | null
  chg1d: number | null
  chg7d: number | null
  chg30d: number | null
  spark: number[]
}

export interface MacroCorr {
  key: string
  name: string
  corr30: number | null
  corr60: number | null
  corr90: number | null
  beta60: number | null
}

export interface MacroResponse {
  series: MacroSeries[]
  correlations: MacroCorr[]
  btc: { last: number } | null
  updatedAt: number
  source: string
  note: string
}

export function fetchMacro(): Promise<MacroResponse> {
  return request<MacroResponse>('/api/macro')
}

// ---- Market scanner ----

export interface ScanRow {
  symbol: string
  last: number
  chg24h: number
  quoteVolume: number
  score: number
  bias: 'bullish' | 'bearish' | 'neutral'
  regime: 'trending' | 'ranging'
  cvdDiv: 'bullish' | 'bearish' | null
  hasPlan: boolean
  topReason: string | null
}

export interface ScanResponse {
  interval: string
  scanned: number
  rows: ScanRow[]
  updatedAt: number
  durationMs: number
  note: string
}

export function fetchScan(interval: string, top = 40): Promise<ScanResponse> {
  return request<ScanResponse>(`/api/scan?interval=${interval}&top=${top}`)
}

// ---- Journal ----

export interface JournalPlan {
  entry?: number
  stop?: number | null
  beR?: number | null
  targetR?: number | null
  trailR?: number | null
  texitBars?: number | null
  fillBars?: number | null
  [k: string]: unknown
}

export interface JournalPlanExit {
  r: number | null
  reason: string
  exitPrice: number | null
  barsHeld: number | null
  beDone?: boolean
}

export interface JournalTrade {
  id: number
  created_at: number
  symbol: string
  interval: string
  direction: 'long' | 'short'
  entry: number
  stop: number | null
  qty: number | null
  leverage: number | null
  opened_at: number | null
  status: 'open' | 'closed'
  closed_at: number | null
  exit_price: number | null
  exit_reason: string | null
  r_multiple: number | null
  plan: JournalPlan | null
  planExit: JournalPlanExit | null
  adherence: string | null
  notes: string | null
}

export interface JournalStats {
  closed: number
  wins?: number
  winRate?: number | null
  nonLossRate?: number | null
  sumR?: number | null
  avgR?: number | null
  adherenceRate?: number | null
  bySymbol?: Record<string, number>
  byInterval?: Record<string, number>
}

export interface JournalTradeInput {
  symbol: string
  interval: string
  direction: 'long' | 'short'
  entry: number
  stop?: number | null
  qty?: number | null
  leverage?: number | null
  openedAt?: number | null
  plan?: JournalPlan | null
  notes?: string | null
}

export function fetchJournalTrades(status?: 'open' | 'closed', limit = 100): Promise<JournalTrade[]> {
  const q = status ? `status=${status}&` : ''
  return request<JournalTrade[]>(`/api/journal/trades?${q}limit=${limit}`)
}

export function createJournalTrade(input: JournalTradeInput): Promise<JournalTrade> {
  return postRequest<JournalTrade>('/api/journal/trades', input)
}

export function closeJournalTrade(
  id: number,
  exit: number,
  reason: string,
): Promise<JournalTrade> {
  return postRequest<JournalTrade>(`/api/journal/trades/${id}/close`, { exit, reason })
}

export function deleteJournalTrade(id: number): Promise<{ ok: boolean }> {
  return deleteRequest<{ ok: boolean }>(`/api/journal/trades/${id}`)
}

export function fetchJournalStats(): Promise<JournalStats> {
  return request<JournalStats>('/api/journal/stats')
}

// ---- Portfolio ----

export interface PortfolioPositionRow {
  symbol: string
  price: number | null
  notionalUsd: number | null
  riskUsd: number | null
  liqPrice: number | null
  unrealizedPct: number | null
  interval: string
  direction: 'long' | 'short'
}

export interface PortfolioAdviceItem {
  level: 'ok' | 'info' | 'warn' | 'danger'
  text: string
}

export interface PortfolioAdvice {
  positions: PortfolioPositionRow[]
  netUsd: number
  grossUsd: number
  marginUsd: number | null
  totalRiskUsd: number | null
  riskPctOfEquity: number | null
  correlatedPairs: { a: string; b: string; corr: number }[]
  betas: Record<string, number>
  items: PortfolioAdviceItem[]
  note: string
}

export interface PortfolioInput {
  positions: {
    symbol: string
    interval: string
    direction: 'long' | 'short'
    entry: number
    stop?: number | null
    qty?: number | null
    leverage?: number | null
    openedAt?: number | null
  }[]
  accountEquity?: number | null
}

export function advisePortfolio(input: PortfolioInput): Promise<PortfolioAdvice> {
  return postRequest<PortfolioAdvice>('/api/portfolio/advise', input)
}

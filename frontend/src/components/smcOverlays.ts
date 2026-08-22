import type { AnalysisResponse } from '../api/client'

const COLORS = {
  obBull: 'rgba(38,166,154,0.16)',
  obBullBorder: 'rgba(38,166,154,0.55)',
  obBear: 'rgba(239,83,80,0.16)',
  obBearBorder: 'rgba(239,83,80,0.55)',
  fvg: 'rgba(255,213,79,0.10)',
  fvgBorder: 'rgba(255,213,79,0.35)',
  buySide: '#ef5350',
  sellSide: '#26a69a',
  bos: '#9aa4b2',
  chochBull: '#26a69a',
  chochBear: '#ef5350',
  eq: 'rgba(154,164,178,0.6)',
  pocLine: 'rgba(240,185,11,0.7)',
  pattern: '#c8a2ff',
  wyckoff: '#4dd0e1',
  sweep: '#f78c6b',
}

const CANDLE_PATTERN_NAMES: Record<string, string> = {
  bullish_engulfing: '吞没↑',
  bearish_engulfing: '吞没↓',
  bullish_pinbar: 'PinBar↑',
  bearish_pinbar: 'PinBar↓',
  inside_bar: '内包',
  morning_star: '晨星',
  evening_star: '暮星',
}

const CHART_PATTERN_NAMES: Record<string, string> = {
  double_top: '双顶',
  double_bottom: '双底',
  head_shoulders_top: '头肩顶',
  head_shoulders_bottom: '头肩底',
  symmetric_triangle: '对称三角',
  ascending_triangle: '上升三角',
  descending_triangle: '下降三角',
}

const WYCKOFF_NAMES: Record<string, string> = {
  spring: 'Spring',
  utad: 'UTAD',
  sos: 'SOS',
}

/** Plain overlay descriptors, decoupled from klinecharts for testability. */
export type OverlaySpec =
  | {
      kind: 'rect'
      id: string
      startTime: number
      top: number
      bottom: number
      color: string
      borderColor: string
      dashed: boolean
      text?: string
    }
  | { kind: 'hline'; id: string; price: number; color: string; dashed: boolean; text?: string }
  | { kind: 'text'; id: string; time: number; price: number; text: string; color: string; bold: boolean }
  | { kind: 'polyline'; id: string; points: { time: number; value: number }[]; color: string }

/** Convert backend SMC analysis into overlay specs for the chart. */
export function buildOverlaySpecs(
  smc: AnalysisResponse['smc'],
  lastClose: number,
  extras?: {
    pocSeries?: { time: number; poc: number }[]
    patterns?: AnalysisResponse['patterns']
    wyckoff?: AnalysisResponse['wyckoff']
  },
): OverlaySpec[] {
  const specs: OverlaySpec[] = []

  // Order blocks (most recent first from backend; draw oldest first so newer are on top)
  for (const ob of [...smc.orderBlocks].reverse()) {
    const bull = ob.type === 'bullish'
    specs.push({
      kind: 'rect',
      id: `ob-${ob.startTime}-${ob.top}`,
      startTime: ob.startTime,
      top: ob.top,
      bottom: ob.bottom,
      color: bull ? COLORS.obBull : COLORS.obBear,
      borderColor: bull ? COLORS.obBullBorder : COLORS.obBearBorder,
      dashed: ob.mitigated,
    })
  }

  // Fair value gaps
  for (const fvg of [...smc.fvgs].reverse()) {
    specs.push({
      kind: 'rect',
      id: `fvg-${fvg.startTime}-${fvg.top}`,
      startTime: fvg.startTime,
      top: fvg.top,
      bottom: fvg.bottom,
      color: COLORS.fvg,
      borderColor: COLORS.fvgBorder,
      dashed: fvg.mitigated,
    })
  }

  // Liquidity pools
  for (const pool of smc.liquidityPools) {
    const color = pool.type === 'buy_side' ? COLORS.buySide : COLORS.sellSide
    specs.push({
      kind: 'hline',
      id: `pool-${pool.type}-${pool.price}`,
      price: pool.price,
      color,
      dashed: pool.swept,
      text: `${pool.type === 'buy_side' ? '买侧' : '卖侧'}流动性${pool.swept ? '·已扫' : ''}`,
    })
  }

  // Structure events (BOS / CHoCH)
  for (const ev of smc.structureEvents.slice(-12)) {
    const isChoch = ev.kind === 'CHoCH'
    const color = isChoch
      ? ev.direction === 'bullish'
        ? COLORS.chochBull
        : COLORS.chochBear
      : COLORS.bos
    // Place bullish markers below price, bearish above.
    const offset = lastClose * 0.004
    specs.push({
      kind: 'text',
      id: `ev-${ev.time}-${ev.kind}-${ev.price}`,
      time: ev.time,
      price: ev.direction === 'bullish' ? ev.price - offset : ev.price + offset,
      text: ev.kind,
      color,
      bold: isChoch,
    })
  }

  // Equilibrium line
  const pd = smc.premiumDiscount
  if (pd && pd.equilibrium > 0) {
    specs.push({
      kind: 'hline',
      id: 'equilibrium',
      price: pd.equilibrium,
      color: COLORS.eq,
      dashed: true,
      text: '均衡位',
    })
  }

  // Developing POC polyline
  if (extras?.pocSeries && extras.pocSeries.length > 1) {
    specs.push({
      kind: 'polyline',
      id: 'poc-series',
      points: extras.pocSeries.map((p) => ({ time: p.time, value: p.poc })),
      color: COLORS.pocLine,
    })
  }

  // Candlestick pattern markers (last few, offset vertically)
  const candlePats = (extras?.patterns?.candles ?? []).slice(-6)
  for (const cp of candlePats) {
    const nm = CANDLE_PATTERN_NAMES[cp.type] ?? cp.type
    specs.push({
      kind: 'text',
      id: `cp-${cp.time}-${cp.type}`,
      time: cp.time,
      price: cp.price,
      text: nm,
      color: cp.direction === 'bullish' ? '#26a69a' : cp.direction === 'bearish' ? '#ef5350' : '#8b949e',
      bold: false,
    })
  }

  // Chart pattern markers
  for (const chp of extras?.patterns?.charts ?? []) {
    const nm = CHART_PATTERN_NAMES[chp.type] ?? chp.type
    const color = chp.direction === 'bullish' ? '#26a69a' : chp.direction === 'bearish' ? '#ef5350' : '#c8a2ff'
    specs.push({
      kind: 'text',
      id: `chartpat-${chp.startTime}-${chp.type}`,
      time: chp.endTime,
      price: chp.keyLevel ?? lastClose,
      text: nm,
      color,
      bold: true,
    })
  }

  // Wyckoff event markers
  for (const ev of extras?.wyckoff?.events ?? []) {
    const nm = WYCKOFF_NAMES[ev.type] ?? ev.type
    specs.push({
      kind: 'text',
      id: `wy-${ev.time}-${ev.type}`,
      time: ev.time,
      price: lastClose,
      text: nm,
      color: COLORS.wyckoff,
      bold: true,
    })
  }

  // Liquidity sweep event markers
  for (const ev of smc.sweepEvents ?? []) {
    const isBuy = ev.side === 'buy_side'
    specs.push({
      kind: 'text',
      id: `sweep-${ev.time}-${ev.side}`,
      time: ev.time,
      price: ev.price,
      text: isBuy ? '扫↑' : '扫↓',
      color: COLORS.sweep,
      bold: false,
    })
  }

  return specs
}

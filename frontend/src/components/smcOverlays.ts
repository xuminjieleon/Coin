import type { AnalysisResponse } from '../api/client'

const COLORS = {
  bos: '#9aa4b2',
  chochBull: '#26a69a',
  chochBear: '#ef5350',
  eq: 'rgba(154,164,178,0.6)',
  pocLine: 'rgba(240,185,11,0.7)',
  wyckoff: '#4dd0e1',
  sweep: '#f78c6b',
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
    wyckoff?: AnalysisResponse['wyckoff']
  },
): OverlaySpec[] {
  const specs: OverlaySpec[] = []

  // Order blocks / FVG zone rectangles are intentionally NOT drawn on the
  // chart (user request 2026-08-24: remove buy-side/sell-side liquidity
  // areas — the filled rects between horizontal lines). The zones remain
  // available in the decision card key levels and the trade plan entry.

  // Liquidity pools: intentionally NOT drawn on the chart (user request
  // 2026-08-24) — pool levels still appear in the key-levels list of the
  // decision card, and sweep events keep their 扫↑/扫↓ markers below.

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

  // Chart-pattern / candlestick-pattern markers REMOVED (round 12b): both
  // sat at weight 0 with consistently negative attribution across
  // calibration rounds. Wyckoff keeps its markers (weighted component).

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

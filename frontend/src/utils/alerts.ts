export interface AlertLevel {
  price: number
  label: string
}

export interface AlertMessage {
  id: string
  time: number
  text: string
  /** optional: toast click switches the app to this symbol */
  symbol?: string
}

const LEVEL_COOLDOWN_MS = 10 * 60 * 1000 // per-level re-fire guard
const PROXIMITY_TOLERANCE = 0.002 // 0.2% counts as "touching"
const PLAN_ALERT_COOLDOWN_MS = 30 * 60 * 1000 // per-symbol plan re-fire guard
const FILL_PROXIMITY_ATR = 0.3 // "price approaching the planned entry" distance
const STEP_MS: Record<string, number> = {
  '1h': 3_600_000,
  '4h': 14_400_000,
  '1d': 86_400_000,
  '1w': 604_800_000,
}

/** scan row subset used by the market-wide plan watcher */
export interface PlanWatchRow {
  symbol: string
  score: number
  bias: string
  hasPlan: boolean
}

interface PlanWatch {
  key: string // symbol|interval|direction
  symbol: string
  interval: string
  direction: 'long' | 'short'
  entry: number
  fillBars: number
  atr: number | null
  firstSeen: number
  alertedNear: boolean
}

/**
 * Local alert engine: watches price against key levels, structure events,
 * the active trade plan (pullback-entry proximity + order-window expiry)
 * and market-wide plan appearances (fed from the scanner).
 * Fires browser notifications (when permitted) + returns messages for toasts.
 */
export class AlertEngine {
  private levels: AlertLevel[] = []
  private lastFiredAt = new Map<string, number>()
  private lastChochTime = 0
  private lastSweepTime = 0
  private enabled = false
  // market-wide plan watcher state
  private seenPlans = new Map<string, string>() // symbol -> direction
  private plansSeeded = false
  private planCooldown = new Map<string, number>() // symbol -> last fire ms
  // current-symbol plan monitor state
  private plan: PlanWatch | null = null

  setEnabled(enabled: boolean): void {
    this.enabled = enabled
  }

  isEnabled(): boolean {
    return this.enabled
  }

  setLevels(levels: AlertLevel[]): void {
    this.levels = levels
  }

  /** Track the current symbol's trade plan (called on every analysis
   * refresh). Plan identity = symbol|interval|direction — entry drifts with
   * ATR between refreshes, which updates in place instead of re-arming. */
  setPlan(
    plan: { direction: 'long' | 'short'; entry: number; fillBars?: number | null } | null,
    symbol: string,
    interval: string,
    atr: number | null,
  ): void {
    if (!plan || !(plan.entry > 0)) {
      this.plan = null
      return
    }
    const key = `${symbol}|${interval}|${plan.direction}`
    if (this.plan && this.plan.key === key) {
      this.plan.entry = plan.entry
      this.plan.fillBars = plan.fillBars ?? this.plan.fillBars
      this.plan.atr = atr ?? this.plan.atr
      return
    }
    this.plan = {
      key,
      symbol,
      interval,
      direction: plan.direction,
      entry: plan.entry,
      fillBars: plan.fillBars ?? 18,
      atr,
      firstSeen: Date.now(),
      alertedNear: false,
    }
  }

  /** Feed scanner rows; returns messages for NEW plans (or direction flips)
   * across the market. First call seeds silently to avoid a notification
   * storm. `currentSymbol` is skipped (already on screen). */
  checkPlans(rows: PlanWatchRow[], interval: string, currentSymbol: string): AlertMessage[] {
    if (!this.enabled) return []
    const out: AlertMessage[] = []
    const current = new Map<string, string>()
    for (const r of rows) {
      if (!r.hasPlan) continue
      current.set(r.symbol, r.score > 0 ? 'long' : 'short')
    }
    if (!this.plansSeeded) {
      this.plansSeeded = true
      this.seenPlans = current
      return out
    }
    const now = Date.now()
    for (const [sym, dir] of current) {
      if (sym === currentSymbol) continue
      const prev = this.seenPlans.get(sym)
      if (prev === dir) continue
      if (now - (this.planCooldown.get(sym) ?? 0) < PLAN_ALERT_COOLDOWN_MS) continue
      this.planCooldown.set(sym, now)
      const row = rows.find((r) => r.symbol === sym)
      const score = row ? row.score : 0
      out.push({
        id: `plan-${sym}-${now}`,
        time: now,
        symbol: sym,
        text: `${sym} ${interval} ${prev === undefined ? '出现交易计划' : '计划转向'}：`
          + `${dir === 'long' ? '做多' : '做空'}（评分 ${score > 0 ? '+' : ''}${score}）`,
      })
    }
    this.seenPlans = current
    return out
  }

  /** Feed structure events; returns new alert messages (CHoCH / sweep). */
  checkEvents(
    structureEvents: { time: number; kind: string; direction: string }[],
    sweepEvents: { time: number; side: string; outcome: string }[],
    symbol: string,
  ): AlertMessage[] {
    if (!this.enabled) return []
    const out: AlertMessage[] = []
    const choch = [...structureEvents].reverse().find((e) => e.kind === 'CHoCH')
    if (choch && choch.time > this.lastChochTime) {
      if (this.lastChochTime > 0) {
        out.push({
          id: `choch-${choch.time}`,
          time: choch.time,
          text: `${symbol} 结构转变（CHoCH ${choch.direction === 'bullish' ? '转多' : '转空'}）`,
        })
      }
      this.lastChochTime = choch.time
    }
    const sweep = sweepEvents[sweepEvents.length - 1]
    if (sweep && sweep.time > this.lastSweepTime) {
      if (this.lastSweepTime > 0) {
        const sideTxt = sweep.side === 'buy_side' ? '上方买方' : '下方卖方'
        const outTxt = sweep.outcome === 'reclaimed' ? '收回（反转信号）' : '突破（延续）'
        out.push({
          id: `sweep-${sweep.time}`,
          time: sweep.time,
          text: `${symbol} ${sideTxt}流动性被扫后${outTxt}`,
        })
      }
      this.lastSweepTime = sweep.time
    }
    return out
  }

  /** Feed a live price; returns messages when a level is touched or crossed,
   * when the current plan's entry zone is approached, or when the plan's
   * order-validity window expires. */
  checkPrice(price: number, symbol: string): AlertMessage[] {
    if (!this.enabled) return []
    const out: AlertMessage[] = []
    const now = Date.now()
    for (const lvl of this.levels) {
      if (lvl.price <= 0) continue
      const dist = Math.abs(price - lvl.price) / lvl.price
      const key = `${lvl.label}-${lvl.price.toFixed(6)}`
      const last = this.lastFiredAt.get(key) ?? 0
      if (dist <= PROXIMITY_TOLERANCE && now - last > LEVEL_COOLDOWN_MS) {
        this.lastFiredAt.set(key, now)
        const above = price > lvl.price
        out.push({
          id: `lvl-${key}-${now}`,
          time: now,
          text: `${symbol} ${above ? '从上方接近' : '从下方接近'} ${lvl.label} ${lvl.price}`,
        })
      }
    }
    // current-symbol plan monitor: entry proximity + order-window expiry
    const p = this.plan
    if (p && p.symbol === symbol && price > 0) {
      const dist = Math.abs(price - p.entry)
      const tol = (p.atr ?? 0) * FILL_PROXIMITY_ATR
      if (!p.alertedNear && tol > 0 && dist <= tol) {
        p.alertedNear = true
        out.push({
          id: `fill-${p.key}-${now}`,
          time: now,
          symbol,
          text: `${symbol} ${p.interval} 回踩接近计划入场区 ${p.entry}`
            + `（距 ${(dist / price * 100).toFixed(2)}%）——挂单注意成交`,
        })
      }
      const step = STEP_MS[p.interval] ?? STEP_MS['1h']
      const barsSeen = (now - p.firstSeen) / step
      if (barsSeen >= p.fillBars) {
        // re-arm: a plan still on screen after its window keeps reminding
        // once per window
        p.firstSeen = now
        p.alertedNear = false
        out.push({
          id: `expire-${p.key}-${now}`,
          time: now,
          symbol,
          text: `${symbol} ${p.interval} 计划挂单窗口（${p.fillBars} 根）已过——按纪律撤单/重估`,
        })
      }
    }
    return out
  }
}

export function canNotify(): boolean {
  return typeof Notification !== 'undefined' && Notification.permission === 'granted'
}

export function requestNotifyPermission(): Promise<boolean> {
  if (typeof Notification === 'undefined') return Promise.resolve(false)
  if (Notification.permission === 'granted') return Promise.resolve(true)
  if (Notification.permission === 'denied') return Promise.resolve(false)
  return Notification.requestPermission().then((p) => p === 'granted')
}

export function pushNotification(title: string, body: string): void {
  if (canNotify()) {
    try {
      new Notification(title, { body })
    } catch {
      /* ignore */
    }
  }
}

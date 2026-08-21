export interface AlertLevel {
  price: number
  label: string
}

export interface AlertMessage {
  id: string
  time: number
  text: string
}

const LEVEL_COOLDOWN_MS = 10 * 60 * 1000 // per-level re-fire guard
const PROXIMITY_TOLERANCE = 0.002 // 0.2% counts as "touching"

/**
 * Local alert engine: watches price against key levels and structure events.
 * Fires browser notifications (when permitted) + returns messages for toasts.
 */
export class AlertEngine {
  private levels: AlertLevel[] = []
  private lastFiredAt = new Map<string, number>()
  private lastChochTime = 0
  private lastSweepTime = 0
  private enabled = false

  setEnabled(enabled: boolean): void {
    this.enabled = enabled
  }

  isEnabled(): boolean {
    return this.enabled
  }

  setLevels(levels: AlertLevel[]): void {
    this.levels = levels
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

  /** Feed a live price; returns messages when a level is touched or crossed. */
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

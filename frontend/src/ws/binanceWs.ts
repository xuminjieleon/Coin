import type { Interval } from '../types'

export interface WsKline {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  isNew: boolean
}

interface BinanceKlineMsg {
  data?: {
    k?: {
      t: number
      o: string
      h: string
      l: string
      c: string
      v: string
    }
  }
}

export type WsStatus = 'connecting' | 'open' | 'closed'

/**
 * Subscribe to Binance futures kline stream. Falls back to the spot stream
 * when the futures socket cannot be reached (blocked network), since candle
 * prices match and only spot klines are available in that environment anyway.
 * Returns an unsubscribe function.
 */
export function subscribeKline(
  symbol: string,
  interval: Interval,
  onKline: (k: WsKline) => void,
  onStatus?: (s: WsStatus) => void,
): () => void {
  const stream = `${symbol.toLowerCase()}@kline_${interval}`
  const urls = [
    `wss://fstream.binance.com/ws/${stream}`,
    `wss://stream.binance.com:9443/ws/${stream}`,
  ]

  let ws: WebSocket | null = null
  let closed = false
  let failTimer: number | undefined
  let urlIndex = 0
  let lastTime = 0

  const cleanup = () => {
    closed = true
    if (failTimer !== undefined) window.clearTimeout(failTimer)
    if (ws) {
      ws.onclose = null
      ws.onerror = null
      ws.onmessage = null
      ws.onopen = null
      try {
        ws.close()
      } catch {
        /* ignore */
      }
      ws = null
    }
  }

  const connectNext = () => {
    if (closed) return
    if (urlIndex >= urls.length) {
      onStatus?.('closed')
      return
    }
    const url = urls[urlIndex]
    urlIndex += 1
    onStatus?.('connecting')
    try {
      ws = new WebSocket(url)
    } catch {
      connectNext()
      return
    }

    // If the socket does not open quickly (network block), try next URL.
    failTimer = window.setTimeout(() => {
      if (ws && ws.readyState !== WebSocket.OPEN) {
        try {
          ws.close()
        } catch {
          /* ignore */
        }
      }
    }, 5000)

    ws.onopen = () => {
      if (failTimer !== undefined) window.clearTimeout(failTimer)
      onStatus?.('open')
    }
    ws.onmessage = (ev: MessageEvent<string>) => {
      try {
        const msg = JSON.parse(ev.data) as BinanceKlineMsg
        const k = msg.data?.k
        if (!k) return
        const t = Number(k.t)
        const isNew = t !== lastTime
        lastTime = t
        onKline({
          time: t,
          open: Number(k.o),
          high: Number(k.h),
          low: Number(k.l),
          close: Number(k.c),
          volume: Number(k.v),
          isNew,
        })
      } catch {
        /* ignore malformed frames */
      }
    }
    ws.onclose = () => {
      if (closed) return
      if (failTimer !== undefined) window.clearTimeout(failTimer)
      if (urlIndex < urls.length) {
        connectNext()
      } else {
        onStatus?.('closed')
      }
    }
    ws.onerror = () => {
      try {
        ws?.close()
      } catch {
        /* ignore */
      }
    }
  }

  connectNext()
  return cleanup
}

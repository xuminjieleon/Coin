export type Interval = '15m' | '1h' | '4h' | '1d'
export const INTERVALS: Interval[] = ['15m', '1h', '4h', '1d']
export const DEFAULT_SYMBOL = 'BTCUSDT'
export const DEFAULT_INTERVAL: Interval = '1h'
export const QUICK_SYMBOLS = [
  { label: 'BTC', symbol: 'BTCUSDT' },
  { label: 'ETH', symbol: 'ETHUSDT' },
  { label: 'SOL', symbol: 'SOLUSDT' },
]

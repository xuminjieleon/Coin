export function formatPrice(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '--'
  const digits = v >= 1000 ? 1 : v >= 100 ? 2 : v >= 1 ? 3 : 6
  return v.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: digits })
}

export function formatUsd(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '--'
  const abs = Math.abs(v)
  if (abs >= 1e9) return `$${(v / 1e9).toFixed(2)}B`
  if (abs >= 1e6) return `$${(v / 1e6).toFixed(2)}M`
  if (abs >= 1e3) return `$${(v / 1e3).toFixed(1)}K`
  return `$${v.toFixed(2)}`
}

export function formatPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '--'
  const sign = v > 0 ? '+' : ''
  return `${sign}${v.toFixed(digits)}%`
}

export function formatFunding(rate: number | null | undefined): string {
  if (rate == null || Number.isNaN(rate)) return '--'
  return `${(rate * 100).toFixed(4)}%`
}

export function formatRatio(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '--'
  return v.toFixed(2)
}

import { useEffect, useRef, useState } from 'react'
import { fetchSymbols, type SymbolInfo } from '../api/client'
import { QUICK_SYMBOLS } from '../types'

interface Props {
  symbol: string
  onSelect: (symbol: string) => void
}

export default function SymbolSearch({ symbol, onSelect }: Props) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SymbolInfo[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<number | undefined>(undefined)

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [])

  useEffect(() => {
    if (timerRef.current !== undefined) window.clearTimeout(timerRef.current)
    if (!query.trim()) {
      setResults([])
      setOpen(false)
      return
    }
    timerRef.current = window.setTimeout(async () => {
      setLoading(true)
      try {
        const list = await fetchSymbols(query.trim())
        setResults(list)
        setOpen(true)
      } catch {
        setResults([])
        setOpen(true)
      } finally {
        setLoading(false)
      }
    }, 300)
    return () => {
      if (timerRef.current !== undefined) window.clearTimeout(timerRef.current)
    }
  }, [query])

  const pick = (s: string) => {
    onSelect(s)
    setQuery('')
    setOpen(false)
  }

  return (
    <div className="symbol-search" ref={boxRef}>
      <div className="quick-symbols">
        {QUICK_SYMBOLS.map((q) => (
          <button
            key={q.symbol}
            className={`quick-btn ${symbol === q.symbol ? 'active' : ''}`}
            onClick={() => pick(q.symbol)}
          >
            {q.label}
          </button>
        ))}
      </div>
      <div className="search-input-wrap">
        <input
          className="search-input"
          placeholder="搜索交易对，如 DOGE"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => results.length > 0 && setOpen(true)}
        />
        {open && (
          <div className="search-dropdown">
            {loading && <div className="search-hint">搜索中…</div>}
            {!loading && results.length === 0 && <div className="search-hint">无匹配结果</div>}
            {results.map((r) => (
              <div key={r.symbol} className="search-item" onMouseDown={() => pick(r.symbol)}>
                <span className="search-item-symbol">{r.symbol}</span>
                <span className="search-item-base">{r.base}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      {!QUICK_SYMBOLS.some((q) => q.symbol === symbol) && (
        <span className="current-custom-symbol" title="当前自选交易对">
          {symbol}
        </span>
      )}
    </div>
  )
}

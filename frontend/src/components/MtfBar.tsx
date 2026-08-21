import type { Mtf, Summary } from '../api/client'

interface Props {
  mtf: Mtf | null
  summary: Summary | null
  interval: string
}

const BIAS_TEXT: Record<string, string> = {
  bullish: '看多',
  bearish: '看空',
  neutral: '中性',
}

const BIAS_CLASS: Record<string, string> = {
  bullish: 'mtf-bullish',
  bearish: 'mtf-bearish',
  neutral: 'mtf-neutral',
}

const ALIGN_TEXT: Record<string, string> = {
  aligned: '多周期共振',
  mixed: '多周期不一',
  conflict: '多周期冲突',
  none: '单周期',
}

const ALIGN_CLASS: Record<string, string> = {
  aligned: 'mtf-align-ok',
  mixed: 'mtf-align-mixed',
  conflict: 'mtf-align-conflict',
  none: 'mtf-align-none',
}

export default function MtfBar({ mtf, summary, interval }: Props) {
  const chips: { label: string; bias: string; score: number }[] = []
  if (summary) {
    chips.push({ label: `${interval} 当前`, bias: summary.bias, score: summary.score })
  }
  for (const tf of mtf?.list ?? []) {
    chips.push({ label: tf.interval.toUpperCase(), bias: tf.bias, score: tf.score })
  }
  const alignment = mtf?.alignment ?? 'none'
  return (
    <div className="mtf-bar">
      <span className="mtf-title">多周期</span>
      {chips.length === 0 && <span className="mtf-empty">加载中…</span>}
      {chips.map((c) => (
        <span key={c.label} className={`mtf-chip ${BIAS_CLASS[c.bias] ?? 'mtf-neutral'}`}>
          <span className="mtf-chip-tf">{c.label}</span>
          <span className="mtf-chip-bias">{BIAS_TEXT[c.bias] ?? c.bias}</span>
          <span className="mtf-chip-score">
            {c.score > 0 ? '+' : ''}
            {c.score}
          </span>
        </span>
      ))}
      {chips.length > 0 && (
        <span className={`mtf-align ${ALIGN_CLASS[alignment] ?? 'mtf-align-none'}`}>
          {ALIGN_TEXT[alignment] ?? alignment}
        </span>
      )}
    </div>
  )
}

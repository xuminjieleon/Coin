import type { AnalysisResponse, BacktestResult } from '../api/client'
import { formatPrice } from '../utils/format'
import SourceHint from './SourceHint'

interface Props {
  analysis: AnalysisResponse | null
  backtest: BacktestResult | null
}

const BIAS_TEXT: Record<string, string> = {
  bullish: '看多',
  bearish: '看空',
  neutral: '中性',
}

const REGIME_TEXT: Record<string, string> = {
  trending: '趋势市',
  ranging: '震荡市',
}

const PD_TEXT: Record<string, string> = {
  premium: '溢价区',
  discount: '折价区',
  equilibrium: '均衡位',
}

const WYCKOFF_TEXT: Record<string, string> = {
  accumulation: '吸筹区间',
  distribution: '派发区间',
  markup: '拉升阶段',
  markdown: '下跌阶段',
}

const VOL_TEXT: Record<string, string> = {
  compressed: '波动压缩',
  normal: '波动正常',
  expanded: '波动放大',
}

function scoreColor(score: number): string {
  if (score >= 15) return '#26a69a'
  if (score <= -15) return '#ef5350'
  return '#8b949e'
}

function ScoreGauge({ score }: { score: number }) {
  const color = scoreColor(score)
  // position: 0% (=-100) .. 100% (=+100)
  const pos = ((score + 100) / 200) * 100
  return (
    <div className="score-gauge">
      <div className="score-value" style={{ color }}>
        {score > 0 ? '+' : ''}
        {score}
      </div>
      <div className="score-bar">
        <div className="score-bar-track" />
        <div className="score-bar-zero" />
        <div className="score-bar-marker" style={{ left: `${pos}%`, backgroundColor: color }} />
      </div>
      <div className="score-bar-labels">
        <span>-100 看空</span>
        <span>看多 +100</span>
      </div>
    </div>
  )
}

export default function DecisionCard({ analysis, backtest }: Props) {
  if (!analysis) {
    return (
      <div className="panel">
        <div className="panel-title-row">
          <div className="panel-title">决策摘要</div>
          <SourceHint
            text="CoinLens 本地分析引擎加权评分：按趋势市/震荡市自动切换权重（结构趋势 30/10、EMA 排列 8/2、多周期共振 10/8、CVD 背离 14/16 及多周期共振、订单块 8/10、资金费率 10/8、OI 10/6、RSI 仅震荡市 0/10、Wyckoff 6/8、磁吸 4/6、溢价折价 2/5；FVG/图表形态/K线形态仅展示不计分——大样本归因为负）。数据源不可达的维度自动跳过。2年×3币种回测：方向胜率长期约 50~61%，请勿将评分当作方向概率使用；可靠的执行优势见「交易计划」卡。仅辅助参考，不构成投资建议。"
          />
        </div>
        <div className="panel-empty">加载中…</div>
      </div>
    )
  }
  const { summary, smc, wyckoff, volatility } = analysis
  const pd = smc.premiumDiscount
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">决策摘要</div>
        <SourceHint
          text="CoinLens 本地分析引擎加权评分：按趋势市/震荡市自动切换权重（结构趋势 30/10、EMA 排列 8/2、多周期共振 10/8、CVD 背离 14/16 及多周期共振、订单块 8/10、资金费率 10/8、OI 10/6、RSI 仅震荡市 0/10、Wyckoff 6/8、磁吸 4/6、溢价折价 2/5；FVG/图表形态/K线形态仅展示不计分——大样本归因为负）。数据源不可达的维度自动跳过。2年×3币种回测：方向胜率长期约 50~61%，请勿将评分当作方向概率使用；可靠的执行优势见「交易计划」卡。仅辅助参考，不构成投资建议。"
        />
      </div>
      <ScoreGauge score={summary.score} />
      <div className="decision-tags">
        <span className={`tag tag-${summary.bias}`}>{BIAS_TEXT[summary.bias] ?? summary.bias}</span>
        <span className="tag tag-neutral">{REGIME_TEXT[summary.regime] ?? summary.regime}</span>
        <span className={`tag tag-${pd.position === 'premium' ? 'bearish' : pd.position === 'discount' ? 'bullish' : 'neutral'}`}>
          {PD_TEXT[pd.position] ?? pd.position}
        </span>
        {wyckoff && wyckoff.phase !== 'none' && (
          <span className={`tag tag-${wyckoff.phase === 'accumulation' || wyckoff.phase === 'markup' ? 'bullish' : 'bearish'}`}>
            {WYCKOFF_TEXT[wyckoff.phase] ?? wyckoff.phase}
          </span>
        )}
        {volatility && (
          <span className={`tag tag-neutral ${volatility.squeeze ? 'tag-squeeze' : ''}`}>
            {VOL_TEXT[volatility.state] ?? volatility.state}
            {volatility.squeeze ? '·挤压' : ''}
          </span>
        )}
        {summary.cvdConfluence && summary.cvdConfluence.direction && summary.cvdConfluence.count >= 2 && (
          <span className={`tag tag-${summary.cvdConfluence.direction}`}>
            CVD背离共振×{summary.cvdConfluence.count}
          </span>
        )}
      </div>
      <div className="pd-progress">
        <div className="pd-progress-track">
          <div className="pd-progress-fill" style={{ width: `${Math.min(100, Math.max(0, pd.pct * 100))}%` }} />
          <div className="pd-progress-mid" />
        </div>
        <div className="pd-progress-labels">
          <span>折价 {formatPrice(pd.rangeLow)}</span>
          <span>均衡 {formatPrice(pd.equilibrium)}</span>
          <span>溢价 {formatPrice(pd.rangeHigh)}</span>
        </div>
      </div>
      <div className="section-label">关键价位</div>
      <div className="key-levels">
        {summary.keyLevels.length === 0 && <div className="panel-empty">无</div>}
        {summary.keyLevels.map((k, i) => (
          <div className="key-level" key={`${k.price}-${i}`}>
            <span className="key-level-label">{k.label}</span>
            <span className="key-level-price">{formatPrice(k.price)}</span>
          </div>
        ))}
      </div>
      <div className="section-label">评分依据</div>
      <div className="reasons">
        {summary.reasons.map((r, i) => (
          <div className="reason" key={i}>
            <span className={`reason-dot reason-dot-${r.direction}`} />
            <span className="reason-text">{r.text}</span>
            <span className={`reason-weight reason-weight-${r.direction}`}>
              {r.weight > 0 ? `+${r.weight}` : r.weight}
            </span>
          </div>
        ))}
      </div>
      {backtest && (
        <div className="backtest-line">
          <span className="backtest-label">历史验证</span>
          <span className="backtest-stats">
            IC {backtest.ic.toFixed(2)}
            {backtest.hitRate != null && ` · 方向胜率 ${(backtest.hitRate * 100).toFixed(0)}%`}
            {` · n=${backtest.directionalSamples}`}
          </span>
          <SourceHint
            text={`滚动回测：以该周期最近 2 年历史 K 线（本地磁盘缓存，首次拉取较慢）复算轻量评分（结构/EMA/RSI/溢价折价/CVD 背离，按趋势/震荡分化权重），统计其与未来 ${backtest.horizon} 根 K 线收益的关系。IC 为评分与未来收益的秩相关（>0.1 有效），胜率为 |评分|≥15 时方向命中率。历史表现不代表未来。`}
          />
        </div>
      )}
    </div>
  )
}

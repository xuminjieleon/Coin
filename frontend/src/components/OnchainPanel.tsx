import type { OnchainResponse } from '../api/client'
import SourceHint, { type SourceLink } from './SourceHint'

interface Props {
  data: OnchainResponse | null
}

const LINKS: SourceLink[] = [
  { label: 'mempool.space', url: 'https://mempool.space' },
  { label: 'blockchain.com 图表', url: 'https://www.blockchain.com/explorer/charts' },
]

function fmtHashrate(v: number | null): string {
  if (v == null) return '--'
  const eh = v / 1e18
  if (eh >= 1000) return `${(v / 1e21).toFixed(1)} ZH/s`
  return `${eh.toFixed(1)} EH/s`
}

function fmtInt(v: number | null): string {
  if (v == null) return '--'
  return Math.round(v).toLocaleString('en-US')
}

export default function OnchainPanel({ data }: Props) {
  const hint =
    'BTC 链上网络健康度：算力与 30 日变化、内存池拥堵、手续费、难度调整周期、活跃地址与链上交易数。交易所净流入/稳定币流向等实体标签数据需要付费源（本网络不可达），如实置空。'
  const btc = data?.btc
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">链上数据（BTC）</div>
        <SourceHint text={hint} links={LINKS} />
      </div>
      {!btc ? (
        <div className="panel-empty">加载中…</div>
      ) : (
        <>
          <div className="deriv-grid">
            <div className="deriv-item">
              <div className="deriv-label">全网算力</div>
              <div className="deriv-value">{fmtHashrate(btc.hashrate)}</div>
              <div className={`deriv-sub ${(btc.hashrateChg30d ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
                30d {btc.hashrateChg30d != null ? `${btc.hashrateChg30d >= 0 ? '+' : ''}${btc.hashrateChg30d.toFixed(1)}%` : '--'}
              </div>
            </div>
            <div className="deriv-item">
              <div className="deriv-label">内存池</div>
              <div className="deriv-value">{fmtInt(btc.mempoolTxs)}</div>
              <div className="deriv-sub">
                积压 {btc.mempoolVsize != null ? `${(btc.mempoolVsize / 1e6).toFixed(1)} MvB` : '--'}
              </div>
            </div>
            <div className="deriv-item">
              <div className="deriv-label">手续费</div>
              <div className="deriv-value">{btc.fees ? `${btc.fees.fastest ?? '--'} sat/vB` : '--'}</div>
              <div className="deriv-sub">高速档；1h 档 {btc.fees ? btc.fees.hour : '--'}</div>
            </div>
            <div className="deriv-item">
              <div className="deriv-label">活跃地址</div>
              <div className="deriv-value">{fmtInt(btc.activeAddresses)}</div>
              <div className="deriv-sub">30d 均值 {fmtInt(btc.activeAddrAvg30d)}</div>
            </div>
          </div>
          {btc.difficulty && (
            <div className="onchain-diff">
              <div className="section-label">难度调整周期</div>
              <div className="onchain-diff-row">
                <span>进度 {btc.difficulty.progressPct != null ? `${btc.difficulty.progressPct.toFixed(1)}%` : '--'}</span>
                <span className={(btc.difficulty.difficultyChangePct ?? 0) >= 0 ? 'text-up' : 'text-down'}>
                  预期调整 {btc.difficulty.difficultyChangePct != null ? `${btc.difficulty.difficultyChangePct >= 0 ? '+' : ''}${btc.difficulty.difficultyChangePct.toFixed(2)}%` : '--'}
                </span>
                <span>剩 {fmtInt(btc.difficulty.remainingBlocks)} 块</span>
              </div>
            </div>
          )}
          <div className="pos-note onchain-note">{data?.unavailable}</div>
        </>
      )}
    </div>
  )
}

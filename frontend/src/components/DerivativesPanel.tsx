import type { Derivatives } from '../api/client'
import { formatUsd, formatPct, formatFunding, formatRatio } from '../utils/format'
import SourceHint, { type SourceLink } from './SourceHint'

interface Props {
  derivatives: Derivatives | null
  symbol: string
}

function sourceLinks(symbol: string): SourceLink[] {
  const base = symbol.replace(/USDT$/, '')
  return [
    { label: 'Coinglass 持仓量', url: `https://www.coinglass.com/open-interest/${base}` },
    { label: 'Coinglass 资金费率', url: 'https://www.coinglass.com/FundingRate' },
    { label: 'Coinglass 多空比', url: 'https://www.coinglass.com/LongShortRatio' },
    { label: '币安合约盘', url: `https://www.binance.com/en/futures/${symbol}` },
  ]
}

function FundingMiniChart({ history }: { history: NonNullable<Derivatives['fundingHistory']> }) {
  const data = history.slice(-30)
  if (data.length === 0) return null
  const maxAbs = Math.max(...data.map((d) => Math.abs(d.rate)), 1e-8)
  const w = 100 / data.length
  return (
    <svg className="funding-chart" viewBox="0 0 100 40" preserveAspectRatio="none">
      <line x1="0" y1="20" x2="100" y2="20" className="funding-chart-zero" />
      {data.map((d, i) => {
        const h = (Math.abs(d.rate) / maxAbs) * 18
        const y = d.rate >= 0 ? 20 - h : 20
        return (
          <rect
            key={d.time}
            x={i * w + w * 0.15}
            y={y}
            width={w * 0.7}
            height={Math.max(h, 0.5)}
            className={d.rate >= 0 ? 'funding-bar-pos' : 'funding-bar-neg'}
          />
        )
      })}
    </svg>
  )
}

function isAllNull(d: Derivatives): boolean {
  return (
    d.openInterest == null &&
    d.fundingRate == null &&
    d.longShortRatio == null &&
    d.takerBuySellRatio == null &&
    !d.fundingHistory?.length
  )
}

export default function DerivativesPanel({ derivatives, symbol }: Props) {
  const links = sourceLinks(symbol)
  const sourceText =
    derivatives?.source === 'gateio'
      ? '当前数据来自 Gate.io 合约 API（币安合约接口不可达时自动切换）。'
      : '数据来自币安合约 API（fapi.binance.com）。'
  if (!derivatives) {
    return (
      <div className="panel">
        <div className="panel-title-row">
          <div className="panel-title">衍生品资金流</div>
          <SourceHint text={`${sourceText}点击右侧链接可在网页端查看同源数据。`} links={links} />
        </div>
        <div className="panel-empty">加载中…</div>
      </div>
    )
  }
  if (isAllNull(derivatives)) {
    return (
      <div className="panel">
        <div className="panel-title-row">
          <div className="panel-title">衍生品资金流</div>
          <SourceHint text={`${sourceText}点击右侧链接可在网页端查看同源数据。`} links={links} />
        </div>
        <div className="panel-empty">
          合约数据源不可达（币安与 Gate.io 均失败）
          <span className="panel-empty-links">
            {links.map((l) => (
              <a key={l.url} href={l.url} target="_blank" rel="noreferrer">
                {l.label} ↗
              </a>
            ))}
          </span>
        </div>
      </div>
    )
  }

  const funding = derivatives.fundingRate
  const fundingHot = funding != null && funding > 0.0005
  const fundingCold = funding != null && funding < -0.0005

  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">衍生品资金流</div>
        {derivatives.source && <span className="source-badge">{derivatives.source === 'gateio' ? 'Gate.io' : '币安'}</span>}
        <SourceHint
          text={`${sourceText}持仓量与多空比来自合约统计接口，资金费率为最新一期结算值。点击右侧链接可在网页端查看同源数据。`}
          links={links}
        />
      </div>
      <div className="deriv-grid">
        <div className="deriv-item">
          <div className="deriv-label">持仓量 OI</div>
          <div className="deriv-value">{formatUsd(derivatives.openInterestValue)}</div>
          <div
            className={`deriv-sub ${(derivatives.oiChangePct24h ?? 0) >= 0 ? 'text-up' : 'text-down'}`}
          >
            24h {formatPct(derivatives.oiChangePct24h)}
          </div>
        </div>
        <div className="deriv-item">
          <div className="deriv-label">资金费率</div>
          <div className={`deriv-value ${fundingHot ? 'text-down' : fundingCold ? 'text-up' : ''}`}>
            {formatFunding(funding)}
          </div>
          <div className="deriv-sub">{fundingHot ? '过热' : fundingCold ? '偏冷' : '正常'}</div>
        </div>
        <div className="deriv-item">
          <div className="deriv-label">多空比</div>
          <div className="deriv-value">{formatRatio(derivatives.longShortRatio)}</div>
          <div className="deriv-sub">全局账户</div>
        </div>
        <div className="deriv-item">
          <div className="deriv-label">主动买卖比</div>
          <div className="deriv-value">{formatRatio(derivatives.takerBuySellRatio)}</div>
          <div className="deriv-sub">Taker</div>
        </div>
      </div>
      {derivatives.fundingHistory && derivatives.fundingHistory.length > 0 && (
        <div className="funding-chart-wrap">
          <div className="section-label">资金费率历史（近30期）</div>
          <FundingMiniChart history={derivatives.fundingHistory} />
        </div>
      )}
      {derivatives.options && (derivatives.options.atmIv != null || derivatives.options.putCallRatio != null) && (
        <div className="options-card">
          <div className="section-label">期权市场（Gate.io，最近到期月）</div>
          <div className="deriv-grid">
            <div className="deriv-item">
              <div className="deriv-label">ATM 隐含波动率</div>
              <div className="deriv-value">
                {derivatives.options.atmIv != null
                  ? `${(derivatives.options.atmIv * 100).toFixed(1)}%`
                  : '--'}
              </div>
              <div className="deriv-sub">
                {derivatives.options.atmIv != null && derivatives.options.atmIv > 0.8 ? '偏高（期权卖方活跃）' : '正常'}
              </div>
            </div>
            <div className="deriv-item">
              <div className="deriv-label">Put/Call 比（OI）</div>
              <div
                className={`deriv-value ${(derivatives.options.putCallRatio ?? 1) > 1.2 ? 'text-down' : (derivatives.options.putCallRatio ?? 1) < 0.8 ? 'text-up' : ''}`}
              >
                {derivatives.options.putCallRatio != null
                  ? derivatives.options.putCallRatio.toFixed(2)
                  : '--'}
              </div>
              <div className="deriv-sub">
                {(derivatives.options.putCallRatio ?? 1) > 1.2
                  ? '看跌偏重'
                  : (derivatives.options.putCallRatio ?? 1) < 0.8
                    ? '看涨偏重'
                    : '均衡'}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

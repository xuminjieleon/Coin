import type { TradePlan } from '../api/client'
import { formatPrice } from '../utils/format'
import SourceHint from './SourceHint'

interface Props {
  plan: TradePlan | null
}

const PLAN_BACKTEST_NOTE =
  '回测口径（走样本前向验证，防过拟合）：BTC/ETH/SOL 各 1000 个决策点（2024-10 ~ 2026-07，共 3000 点），' +
  '按时序切 40%/30%/30% 三段：A 段调参 → B 段盲测 → A+B 重调 → C 段盲测。两个阶段独立选出了同一组参数。' +
  '超时单按市价结算（严格口径）。结果：A 段 93.1%、B 段盲测 94.9%、A+B 重调 93.8%、C 段盲测 91.0% 非亏损率' +
  '（盈利+保本离场）；C 段抽稀独立性校验 90.4~91.2%，分币种 90.1~92.3%；每笔期望 +0.15R（成交口径）。' +
  '历史表现不代表未来，仅供参考。'

export default function TradePlanCard({ plan }: Props) {
  if (!plan) {
    return (
      <div className="panel">
        <div className="panel-title-row">
          <div className="panel-title">交易计划</div>
          <SourceHint
            text="评分达到 ±25 或 CVD 多周期背离共振时生成：回踩 0.75×ATR 限价入场、1.5×ATR 止损、0.75R/1.5R 分批目标，+0.25R 即保本移损，96 根 K 线未触发市价离场。仅供参考，请自行评估风险。"
          />
        </div>
        <div className="panel-empty">当前信号强度不足，暂无结构化计划</div>
      </div>
    )
  }
  const long = plan.direction === 'long'
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">交易计划</div>
        <SourceHint
          text={`评分达到 ±25 或 CVD 多周期背离共振时生成：回踩 0.75×ATR 限价入场、1.5×ATR 止损、0.75R/1.5R 分批目标，+0.25R 即保本移损，96 根 K 线未触发市价离场。${PLAN_BACKTEST_NOTE}`}
        />
      </div>
      <div className="tp-direction">
        <span className={`tp-badge ${long ? 'tp-long' : 'tp-short'}`}>{long ? '做多 LONG' : '做空 SHORT'}</span>
        <span className="tp-rr">目标盈亏比 0.75R / 1.5R</span>
      </div>
      <div className="tp-grid">
        <div className="tp-item">
          <span className="tp-label">入场（回踩限价）</span>
          <span className="tp-value">{formatPrice(plan.entry)}</span>
        </div>
        <div className="tp-item">
          <span className="tp-label">止损（1.5×ATR）</span>
          <span className="tp-value tp-stop">{formatPrice(plan.stop)}</span>
        </div>
        <div className="tp-item">
          <span className="tp-label">目标 1（0.75R）</span>
          <span className="tp-value tp-target">{formatPrice(plan.target1)}</span>
        </div>
        <div className="tp-item">
          <span className="tp-label">目标 2（1.5R）</span>
          <span className="tp-value tp-target">{formatPrice(plan.target2)}</span>
        </div>
        {plan.beTrigger != null && (
          <div className="tp-item tp-item-wide">
            <span className="tp-label">保本移损触发（+0.25R，触发后止损移至入场价）</span>
            <span className="tp-value tp-be">{formatPrice(plan.beTrigger)}</span>
          </div>
        )}
      </div>
      <div className="tp-note">{plan.note}</div>
      <div className="tp-backtest-line">
        <span className="backtest-label">计划回测</span>
        <span className="backtest-stats">非亏损率 ~91%（走样本盲测 B 段 94.9% / C 段 91.0%，含保本管理）</span>
      </div>
    </div>
  )
}

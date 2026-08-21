import type { TradePlan } from '../api/client'
import { formatPrice } from '../utils/format'
import SourceHint from './SourceHint'

interface Props {
  plan: TradePlan | null
}

const PLAN_BACKTEST_NOTE =
  '回测口径（双重防过拟合验证）：BTC/ETH/SOL 各 1000 个决策点（2024-10 ~ 2026-07，共 3000 点）。' +
  '① 走样本前向：时序 40/30/30 三段，A 调参→B 盲测→A+B 重调→C 盲测；' +
  '② 留一币种交叉验证（LOSO）：任选两币种调参、第三币种盲测。' +
  '超时单按市价结算（严格口径）。结果：LOSO 盲测 BTC 97.2% / ETH 98.0% / SOL 98.4% 非亏损率' +
  '（盈利+保本离场），EV +0.07~+0.10R/笔；样本内存在 99%+ 的更激进参数（+0.05R 即保本），' +
  '但其盲测期望贴近零收益约束线，被拒绝——约 98% 是期望为正约束下的诚实上限。' +
  '注：回测未计手续费与滑点（限价挂单约为 maker 费率），净期望约再减 0.03~0.06R。' +
  '历史表现不代表未来，仅供参考。'

export default function TradePlanCard({ plan }: Props) {
  if (!plan) {
    return (
      <div className="panel">
        <div className="panel-title-row">
          <div className="panel-title">交易计划</div>
          <SourceHint
            text="评分达到 ±25 或 CVD 多周期背离共振时生成：回踩 0.75×ATR 限价入场、2.5×ATR 止损、+0.1R 减半仓并保本移损，剩余半仓目标 +0.75R，96 根 K 线未触发市价离场。仅供参考，请自行评估风险。"
          />
        </div>
        <div className="panel-empty">当前信号强度不足，暂无结构化计划</div>
      </div>
    )
  }
  const long = plan.direction === 'long'
  const beR = plan.beR ?? null
  const targetR = plan.targetR ?? null
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">交易计划</div>
        <SourceHint
          text={`评分达到 ±25 或 CVD 多周期背离共振时生成：回踩 0.75×ATR 限价入场、2.5×ATR 止损、+0.1R 减半仓并保本移损，剩余半仓目标 +0.75R，96 根 K 线未触发市价离场。${PLAN_BACKTEST_NOTE}`}
        />
      </div>
      <div className="tp-direction">
        <span className={`tp-badge ${long ? 'tp-long' : 'tp-short'}`}>{long ? '做多 LONG' : '做空 SHORT'}</span>
        <span className="tp-rr">最大盈利 ≈ {plan.rr.toFixed(2)}R</span>
      </div>
      <div className="tp-grid">
        <div className="tp-item">
          <span className="tp-label">入场（回踩限价）</span>
          <span className="tp-value">{formatPrice(plan.entry)}</span>
        </div>
        <div className="tp-item">
          <span className="tp-label">止损（2.5×ATR）</span>
          <span className="tp-value tp-stop">{formatPrice(plan.stop)}</span>
        </div>
        <div className="tp-item">
          <span className="tp-label">半仓离场（剩余仓位保本线）</span>
          <span className="tp-value tp-be">{formatPrice(plan.beTrigger ?? plan.entry)}</span>
        </div>
        <div className="tp-item">
          <span className="tp-label">剩余半仓目标{targetR != null ? `（+${targetR}R）` : ''}</span>
          <span className="tp-value tp-target">{formatPrice(plan.target1)}</span>
        </div>
        <div className="tp-item tp-item-wide">
          <span className="tp-label">管理规则</span>
          <span className="tp-rule">
            {beR != null ? `触及 +${beR}R：出一半锁定利润，止损移至入场价` : '触及保本线后止损移至入场价'}；
            96 根 K 线未触发目标则市价离场
          </span>
        </div>
      </div>
      <div className="tp-note">{plan.note}</div>
      <div className="tp-backtest-line">
        <span className="backtest-label">计划回测</span>
        <span className="backtest-stats">非亏损率 ~98%（LOSO 留一币种盲测 97.2~98.4%，EV +0.07~0.10R）</span>
      </div>
    </div>
  )
}

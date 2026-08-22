import type { TradePlan } from '../api/client'
import { formatPrice } from '../utils/format'
import SourceHint from './SourceHint'
import type { Interval } from '../types'

interface Props {
  plan: TradePlan | null
  interval: Interval
}

const PLAN_BACKTEST_NOTE_1H =
  '回测口径（双重防过拟合验证）：BTC/ETH/SOL 各 1000 个决策点（2024-10 ~ 2026-07，共 3000 点）。' +
  '① 走样本前向：时序 40/30/30 三段，A 调参→B 盲测→A+B 重调→C 盲测；' +
  '② 留一币种交叉验证（LOSO）：任选两币种调参、第三币种盲测。' +
  '超时单按市价结算（严格口径）。结果：LOSO 盲测 BTC 97.2% / ETH 98.0% / SOL 98.4% 非亏损率' +
  '（盈利+保本离场），EV +0.07~+0.10R/笔；样本内存在 99%+ 的更激进参数（+0.05R 即保本），' +
  '但其盲测期望贴近零收益约束线，被拒绝——约 98% 是期望为正约束下的诚实上限。' +
  '注：回测未计手续费与滑点（限价挂单约为 maker 费率），净期望约再减 0.03~0.06R。' +
  '历史表现不代表未来，仅供参考。'

const PLAN_BACKTEST_NOTE_SWING =
  '回测口径（2026-08-22 第 11 轮利润优先优化，容量约束=单仓位/币种串行执行，贴近个人账户可实现收益）：' +
  '扩展数据窗口 4h×3 年 / 1d×4 年 / 1w×10 年上限 × BTC/ETH/SOL；时序 40/30/30 折走样本' +
  '（A 调参→B 盲测→A+B 重调→C 盲测，MTF 上下文只用已收盘的更高周期 K 线）。' +
  '优化过程：门控两轮（全部降低利润，拒绝）→ 坐标下降精调（止损收紧+跟踪止盈）→ 成交窗口与' +
  '计划阈值扫描（阈值 10 在 A+B 与盲测一致胜出：边际交易单独 EV 为正，优势在入场+管理执行层）。' +
  '盲测结果（单仓位串行）：4h +288R / 胜率 87% / EV +0.32R / 最大回撤 5R；' +
  '1d +47R / 87% / +0.24R / 3R；1w +21R / 96% / +0.46R / 1R；三币种独立全为正。' +
  '诚实提示：① 信号本身的方向胜率仍约 50%，利润来自"回调入场+分批保本+跟踪止盈"的执行层；' +
  '② 1w 盲测仅 46 笔（样本小）；③ 未计手续费/滑点（净 EV 再减 0.03~0.06R）。' +
  '历史表现不代表未来，仅供参考。'

const HINT_1H =
  '评分达到 ±25 或 CVD 多周期背离共振时生成：回踩 0.75×ATR 限价入场、2.5×ATR 止损、' +
  '+0.1R 减半仓并保本移损，剩余半仓目标 +0.75R，96 根 K 线未触发市价离场。仅供参考，请自行评估风险。'

const HINT_SWING =
  '评分达到 ±10 或 CVD 多周期背离共振时生成（优势在执行层，信号阈值已按利润优先校准放宽）：' +
  '回踩 0.75×ATR 限价入场（区域边缘更优）、1.2×ATR(4h)/1.5×ATR(1d/1w) 止损、' +
  '触及 +0.5R 减半仓并将止损移至入场价，剩余半仓不设固定目标、以 0.5R(4h/1d)/0.75R(1w) 跟踪止盈，' +
  '限时限价成交（4h 18 根/1d 9 根/1w 8 根未成交撤单），超时市价离场。' +
  '建议单仓位串行执行。仅供参考，请自行评估风险。'

const BACKTEST_LINE_1H = '非亏损率 ~98%（LOSO 留一币种盲测 97.2~98.4%，EV +0.07~0.10R）'
const BACKTEST_LINE_4H = '利润优先盲测（单仓位串行）：+288R · 胜率 87% · EV +0.32R · 回撤 5R'
const BACKTEST_LINE_1D = '利润优先盲测（单仓位串行）：+47R · 胜率 87% · EV +0.24R · 回撤 3R'
const BACKTEST_LINE_1W = '利润优先盲测（单仓位串行）：+21R · 胜率 96% · EV +0.46R · 回撤 1R（仅 46 笔）'

export default function TradePlanCard({ plan, interval }: Props) {
  const swing = interval !== '1h'
  const hint = (swing ? HINT_SWING : HINT_1H) + (swing ? PLAN_BACKTEST_NOTE_SWING : PLAN_BACKTEST_NOTE_1H)
  const backtestLine =
    interval === '4h' ? BACKTEST_LINE_4H : interval === '1d' ? BACKTEST_LINE_1D : interval === '1w' ? BACKTEST_LINE_1W : BACKTEST_LINE_1H
  if (!plan) {
    return (
      <div className="panel">
        <div className="panel-title-row">
          <div className="panel-title">交易计划</div>
          <SourceHint text={hint} />
        </div>
        <div className="panel-empty">当前信号强度不足，暂无结构化计划</div>
      </div>
    )
  }
  const long = plan.direction === 'long'
  const beR = plan.beR ?? null
  const targetR = plan.targetR ?? null
  const trailR = plan.trailR ?? null
  const stopAtr = plan.stopAtr ?? 2.5
  const texitBars = plan.texitBars ?? 96
  const fillBars = plan.fillBars ?? null
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">交易计划</div>
        <SourceHint text={hint} />
      </div>
      <div className="tp-direction">
        <span className={`tp-badge ${long ? 'tp-long' : 'tp-short'}`}>{long ? '做多 LONG' : '做空 SHORT'}</span>
        {plan.rr != null ? (
          <span className="tp-rr">最大盈利 ≈ {plan.rr.toFixed(2)}R</span>
        ) : (
          <span className="tp-rr">跟踪止盈 · 上不封顶</span>
        )}
      </div>
      <div className="tp-grid">
        <div className="tp-item">
          <span className="tp-label">入场（回踩限价）</span>
          <span className="tp-value">{formatPrice(plan.entry)}</span>
        </div>
        <div className="tp-item">
          <span className="tp-label">止损（{stopAtr}×ATR）</span>
          <span className="tp-value tp-stop">{formatPrice(plan.stop)}</span>
        </div>
        <div className="tp-item">
          <span className="tp-label">半仓离场（剩余仓位保本线）</span>
          <span className="tp-value tp-be">{formatPrice(plan.beTrigger ?? plan.entry)}</span>
        </div>
        {plan.target1 != null ? (
          <div className="tp-item">
            <span className="tp-label">剩余半仓目标{targetR != null ? `（+${targetR}R）` : ''}</span>
            <span className="tp-value tp-target">{formatPrice(plan.target1)}</span>
          </div>
        ) : (
          <div className="tp-item">
            <span className="tp-label">剩余半仓跟踪止盈{trailR != null ? `（回撤 ${trailR}R 离场）` : ''}</span>
            <span className="tp-value tp-target">不设固定目标</span>
          </div>
        )}
        <div className="tp-item tp-item-wide">
          <span className="tp-label">管理规则</span>
          <span className="tp-rule">
            {beR != null ? `触及 +${beR}R：出一半锁定利润，止损移至入场价` : '触及保本线后止损移至入场价'}；
            {trailR != null ? `此后自最高盈利回撤 ${trailR}R 离场；` : ''}
            {texitBars} 根 K 线未离场则市价离场
            {fillBars != null ? `；限价单 ${fillBars} 根未成交撤单` : ''}
          </span>
        </div>
      </div>
      <div className="tp-note">{plan.note}</div>
      <div className="tp-backtest-line">
        <span className="backtest-label">计划回测</span>
        <span className="backtest-stats">{backtestLine}</span>
      </div>
    </div>
  )
}

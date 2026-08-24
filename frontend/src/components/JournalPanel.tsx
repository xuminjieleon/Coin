import { useCallback, useEffect, useState } from 'react'
import {
  closeJournalTrade,
  createJournalTrade,
  deleteJournalTrade,
  fetchJournalStats,
  fetchJournalTrades,
  type JournalStats,
  type JournalTrade,
  type TradePlan,
} from '../api/client'
import { formatPrice } from '../utils/format'
import SourceHint from './SourceHint'
import type { Interval } from '../types'

interface Props {
  symbol: string
  interval: Interval
  plan: TradePlan | null
  /** refresh trigger: re-fetch open trades on data refresh */
  refreshKey: number | null
}

const REASON_LABEL: Record<string, string> = {
  stop: '止损', be_stop: '保本/跟踪', target: '目标', trail: '跟踪止盈',
  time: '时间退出', manual: '手动', censored_at_close: '未走完',
  open: '未了结', unfilled: '未成交', invalid: '无效',
}

const ADHERENCE_CLASS: Record<string, string> = {
  followed: 'text-up',
  deviated: 'text-down',
}

export default function JournalPanel({ symbol, interval, plan, refreshKey }: Props) {
  const [trades, setTrades] = useState<JournalTrade[]>([])
  const [stats, setStats] = useState<JournalStats | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [closing, setClosing] = useState<Record<number, { exit: string; reason: string }>>({})
  const [note, setNote] = useState('')

  const reload = useCallback(async () => {
    try {
      const [ts, st] = await Promise.all([fetchJournalTrades(undefined, 100), fetchJournalStats()])
      setTrades(ts)
      setStats(st)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload, refreshKey])

  const addFromPlan = useCallback(async () => {
    if (!plan) return
    setErr(null)
    try {
      await createJournalTrade({
        symbol,
        interval,
        direction: plan.direction,
        entry: plan.entry,
        stop: plan.stop,
        openedAt: Date.now(),
        plan: {
          entry: plan.entry,
          stop: plan.stop,
          beR: plan.beR ?? null,
          targetR: plan.targetR ?? null,
          trailR: plan.trailR ?? null,
          texitBars: plan.texitBars ?? null,
          fillBars: plan.fillBars ?? null,
        },
        notes: note || null,
      })
      setNote('')
      await reload()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [plan, symbol, interval, note, reload])

  const addManual = useCallback(async () => {
    setErr(null)
    const dir = plan?.direction ?? 'long'
    const entry = prompt(`入场价（${dir === 'long' ? '做多' : '做空'} ${symbol} ${interval}）`)
    if (!entry) return
    const entryNum = Number(entry)
    if (!Number.isFinite(entryNum) || entryNum <= 0) {
      setErr('入场价无效')
      return
    }
    try {
      await createJournalTrade({
        symbol,
        interval,
        direction: dir,
        entry: entryNum,
        openedAt: Date.now(),
        notes: note || null,
      })
      setNote('')
      await reload()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [plan, symbol, interval, note, reload])

  const doClose = useCallback(
    async (id: number) => {
      const c = closing[id]
      if (!c) return
      const exitNum = Number(c.exit)
      if (!Number.isFinite(exitNum) || exitNum <= 0) {
        setErr('平仓价无效')
        return
      }
      setErr(null)
      try {
        await closeJournalTrade(id, exitNum, c.reason || 'manual')
        setClosing((s) => {
          const next = { ...s }
          delete next[id]
          return next
        })
        await reload()
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e))
      }
    },
    [closing, reload],
  )

  const doDelete = useCallback(
    async (id: number) => {
      setErr(null)
      try {
        await deleteJournalTrade(id)
        await reload()
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e))
      }
    },
    [reload],
  )

  const open = trades.filter((t) => t.status === 'open')
  const closed = trades.filter((t) => t.status === 'closed')

  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">交易日记与计划遵循</div>
        <SourceHint
          text={`记录每笔交易并冻结开仓时的计划几何；平仓后自动用本地 K 线重放"计划会怎么管理这笔单"（止损→+beR 减半保本→跟踪/目标→时间退出，保守盘口顺序），对比实际离场得出遵循判定。这是把回测验证的执行层优势（+0.5R 保本管理等）转化为实盘收益的关键闭环。`}
        />
      </div>

      <div className="journal-actions">
        <button
          className="journal-add-btn"
          onClick={() => void addFromPlan()}
          disabled={!plan}
          title={plan ? `以当前计划挂单：${plan.direction === 'long' ? '做多' : '做空'} @ ${formatPrice(plan.entry)}` : '当前盘面无交易计划'}
          type="button"
        >
          按当前计划记录
        </button>
        <button className="journal-add-manual" onClick={() => void addManual()} type="button">
          手动记录
        </button>
        <input
          className="journal-note"
          placeholder="备注（可选）"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </div>

      {err && <div className="pos-error">{err}</div>}

      {stats && stats.closed > 0 && (
        <div className="journal-stats">
          <span>
            已平 <b>{stats.closed}</b>
          </span>
          <span>
            胜率 <b>{stats.winRate ?? '--'}%</b>
          </span>
          <span>
            非亏损率 <b>{stats.nonLossRate ?? '--'}%</b>
          </span>
          <span className={(stats.sumR ?? 0) >= 0 ? 'text-up' : 'text-down'}>
            合计 <b>{(stats.sumR ?? 0) >= 0 ? '+' : ''}{stats.sumR}R</b>
          </span>
          <span>
            均值 <b>{(stats.avgR ?? 0) >= 0 ? '+' : ''}{stats.avgR}R</b>
          </span>
          <span title="实际离场与计划重放一致（同因或 ±0.5R 内）的比例">
            计划遵循 <b>{stats.adherenceRate ?? '--'}%</b>
          </span>
        </div>
      )}

      {open.length > 0 && (
        <div className="journal-section">
          <div className="section-label">持仓中（{open.length}）</div>
          {open.map((t) => (
            <div className="journal-trade" key={t.id}>
              <div className="journal-trade-head">
                <span className={t.direction === 'long' ? 'text-up' : 'text-down'}>
                  {t.direction === 'long' ? '多' : '空'} {t.symbol.replace(/USDT$/, '')}
                </span>
                <span>{t.interval}</span>
                <span>入 {formatPrice(t.entry)}</span>
                {t.stop != null && <span>损 {formatPrice(t.stop)}</span>}
                {t.leverage != null && t.leverage > 1 && <span>{t.leverage}×</span>}
                <span className="journal-del" onClick={() => void doDelete(t.id)} title="删除">
                  ✕
                </span>
              </div>
              <div className="journal-close-row">
                <input
                  type="number"
                  placeholder="平仓价"
                  step="any"
                  value={closing[t.id]?.exit ?? ''}
                  onChange={(e) =>
                    setClosing((s) => ({
                      ...s,
                      [t.id]: { exit: e.target.value, reason: s[t.id]?.reason ?? 'manual' },
                    }))
                  }
                />
                <select
                  value={closing[t.id]?.reason ?? 'manual'}
                  onChange={(e) =>
                    setClosing((s) => ({
                      ...s,
                      [t.id]: { exit: s[t.id]?.exit ?? '', reason: e.target.value },
                    }))
                  }
                >
                  <option value="manual">手动</option>
                  <option value="stop">止损</option>
                  <option value="target">目标</option>
                  <option value="trail">跟踪止盈</option>
                  <option value="time">时间退出</option>
                </select>
                <button type="button" onClick={() => void doClose(t.id)}>
                  平仓并复盘
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {closed.length > 0 && (
        <div className="journal-section">
          <div className="section-label">已平仓（最近 {Math.min(closed.length, 20)}）</div>
          {closed.slice(0, 20).map((t) => (
            <div className="journal-trade journal-trade-closed" key={t.id}>
              <div className="journal-trade-head">
                <span className={t.direction === 'long' ? 'text-up' : 'text-down'}>
                  {t.direction === 'long' ? '多' : '空'} {t.symbol.replace(/USDT$/, '')}
                </span>
                <span>{t.interval}</span>
                <span>入 {formatPrice(t.entry)}</span>
                <span>出 {formatPrice(t.exit_price ?? 0)}</span>
                <span className={(t.r_multiple ?? 0) >= 0 ? 'text-up' : 'text-down'}>
                  {(t.r_multiple ?? 0) >= 0 ? '+' : ''}
                  {t.r_multiple?.toFixed(2)}R
                </span>
                <span className={ADHERENCE_CLASS[t.adherence ?? ''] ?? ''}>
                  {t.adherence === 'followed' ? '遵循计划' : t.adherence === 'deviated' ? '偏离计划' : '--'}
                </span>
                <span className="journal-del" onClick={() => void doDelete(t.id)} title="删除">
                  ✕
                </span>
              </div>
              {t.planExit && (
                <div className="journal-plan-exit">
                  计划重放：{REASON_LABEL[t.planExit.reason] ?? t.planExit.reason} @{' '}
                  {formatPrice(t.planExit.exitPrice)}（
                  {(t.planExit.r ?? 0) >= 0 ? '+' : ''}
                  {t.planExit.r?.toFixed(2)}R
                  {t.planExit.beDone ? '，触发过保本' : ''}）
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {trades.length === 0 && (
        <div className="panel-empty">
          暂无记录。盘面出现交易计划时点「按当前计划记录」，或手动记录已有持仓。
        </div>
      )}
    </div>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchExecutorStatus,
  panicExecutor,
  testExecutor,
  updateExecutorConfig,
  type ExecutorStatus,
} from '../api/client'
import { formatPrice } from '../utils/format'
import SourceHint from './SourceHint'

interface Props {
  /** refresh trigger: re-fetch on data refresh */
  refreshKey: number | null
}

const MODE_LABEL: Record<string, string> = {
  paper: '模拟盘（假钱，真实信号）',
  testnet: '币安测试网',
  live: '实盘（真钱）',
}

const EXIT_LABEL: Record<string, string> = {
  stop: '止损', be_stop: '保本/跟踪止损', target: '目标止盈', time: '时间退出',
  panic: '紧急全撤', plan_gone: '计划消失撤单', flip: '转向撤单', amend: '改单撤换',
  expired: '到期撤单', disabled: '关闭撤单', manual_extern: '场外平仓',
  post_only: '未成交撤单',
}

export default function AutoTradePanel({ refreshKey }: Props) {
  const [st, setSt] = useState<ExecutorStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [risk, setRisk] = useState('')
  const [equity, setEquity] = useState('')
  const [dayLimit, setDayLimit] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [testMsg, setTestMsg] = useState<string | null>(null)
  /** fields the user is editing: never clobbered by the 30s poll */
  const [dirty, setDirty] = useState<Record<string, boolean>>({})
  const pollRef = useRef<number | null>(null)

  const reload = useCallback(async () => {
    try {
      const s = await fetchExecutorStatus()
      setSt(s)
      setErr(null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload, refreshKey])

  // 30s poll while running (fills/closes happen between data refreshes)
  useEffect(() => {
    if (!st?.enabled) {
      if (pollRef.current) window.clearInterval(pollRef.current)
      pollRef.current = null
      return
    }
    if (pollRef.current) window.clearInterval(pollRef.current)
    pollRef.current = window.setInterval(() => void reload(), 30_000)
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current)
    }
  }, [st?.enabled, reload])

  useEffect(() => {
    if (st) {
      // W10 fix: only sync inputs the user is NOT currently editing; deps are
      // the server values (not the `st` object identity) so polls don't
      // reset in-progress edits every 30s
      if (!dirty.risk) setRisk(String(st.riskPct))
      if (!dirty.equity) setEquity(String(st.equityUsd))
      if (!dirty.dayLimit) setDayLimit(String(st.dailyLossLimitR))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [st?.riskPct, st?.equityUsd, st?.dailyLossLimitR])

  const patch = useCallback(
    async (body: Parameters<typeof updateExecutorConfig>[0], label: string) => {
      setBusy(true)
      setErr(null)
      try {
        const s = await updateExecutorConfig(body)
        setSt(s)
        setTestMsg(label ? `${label} 已生效` : null)
      } catch (e) {
        setErr(`${label}: ${e instanceof Error ? e.message : String(e)}`)
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  const onTest = useCallback(async () => {
    setBusy(true)
    setTestMsg(null)
    try {
      const r = await testExecutor()
      setTestMsg(r.note || '连接正常')
    } catch (e) {
      setTestMsg(`失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }, [])

  const onPanic = useCallback(async () => {
    if (!window.confirm('紧急全撤：撤销全部自动交易挂单并市价平掉全部自动持仓，且关闭自动交易。确认执行？')) return
    setBusy(true)
    try {
      const r = await panicExecutor()
      setTestMsg(`紧急全撤完成：处理 ${r.closed} 个仓位`)
      await reload()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [reload])

  if (!st) {
    return (
      <div className="panel">
        <div className="panel-title-row">
          <div className="panel-title">自动交易</div>
        </div>
        {err ? <div className="pos-error">{err}</div> : <div className="text-dim">加载中…</div>}
      </div>
    )
  }

  const modeLabel = MODE_LABEL[st.mode] ?? st.mode
  const live = st.mode === 'live'
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">自动交易{st.enabled ? '（运行中）' : ''}</div>
        <SourceHint
          text="把推送里的交易计划直接接到币安合约执行：与推送/回测同一收盘口径，计划出现→自动挂限价入场单（post-only 保证 maker，1h 费后存活的关键）+reduce-only 止损单同时挂上（本地进程死掉仓位也不裸奔）；成交后按冻结计划自动管理（触及 +beR 半仓止盈并把止损提到入场价→剩余半仓跟踪止盈/目标→时间退出）；计划消失/转向/改单自动撤改挂单。三种模式：模拟盘（假钱空跑）、币安测试网、实盘。安全护栏：单向持仓校验、并发上限、同币跨周期共享预算减半、单笔风险/名义额上限、日亏停机、紧急全撤。诚实口径：实盘会有回测没有的成本（滑点/资金费率/挂单迟到），预期按回测下界打折。"
        />
      </div>

      <div className="at-head">
        <span className={`at-badge ${live ? 'at-badge-live' : st.mode === 'testnet' ? 'at-badge-test' : 'at-badge-paper'}`}>
          {modeLabel}
        </span>
        {st.paused && <span className="at-badge at-badge-live">日亏停机中</span>}
        {!st.keysSet && !st.dryRun && <span className="at-badge at-badge-live">未配置密钥</span>}
        <button
          type="button"
          className={st.enabled ? 'at-toggle at-toggle-on' : 'at-toggle'}
          disabled={busy}
          onClick={() => void patch({ enabled: !st.enabled }, st.enabled ? '已停止新开仓（已有仓位仍按计划管理）' : '自动交易已开启')}
        >
          {st.enabled ? '停止开新仓' : '开启自动交易'}
        </button>
        <button type="button" className="at-panic" disabled={busy} onClick={() => void onPanic()}>
          紧急全撤
        </button>
      </div>

      <div className="pf-grid">
        <div className="deriv-item">
          <div className="deriv-label">单笔风险</div>
          <div className="deriv-value">{st.riskPct}% 权益</div>
          <div className="deriv-sub">杠杆 {st.leverage}x 逐仓</div>
        </div>
        <div className="deriv-item">
          <div className="deriv-label">今日已实现</div>
          <div className={`deriv-value ${st.todayRealizedR >= 0 ? 'text-up' : 'text-down'}`}>
            {st.todayRealizedR >= 0 ? '+' : ''}{st.todayRealizedR}R
          </div>
          <div className="deriv-sub">日亏停机 −{st.dailyLossLimitR}R</div>
        </div>
        <div className="deriv-item">
          <div className="deriv-label">累计平仓</div>
          <div className={`deriv-value ${(st.closedSumR ?? 0) >= 0 ? 'text-up' : 'text-down'}`}>
            {st.closedCount} 笔 {(st.closedSumR ?? 0) >= 0 ? '+' : ''}{st.closedSumR}R
          </div>
          <div className="deriv-sub">并发上限 {st.maxConcurrent} · {st.symbols.length} 币 × {st.intervals.length} 周期</div>
        </div>
        <div className="deriv-item">
          <div className="deriv-label">状态</div>
          <div className="deriv-value">
            {st.mode !== 'paper' ? (st.reconciled ? '已对账' : '待对账') : '模拟撮合'}
          </div>
          <div className="deriv-sub">下一轮 {st.nextRun ? new Date(st.nextRun).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '—'}</div>
        </div>
      </div>

      {st.lastError && <div className="pos-error">{st.lastError}</div>}
      {err && <div className="pos-error">{err}</div>}
      {testMsg && <div className="at-note">{testMsg}</div>}

      {st.positions.length > 0 && (
        <div className="at-positions">
          {st.positions.map((p) => (
            <div className="at-pos" key={p.id}>
              <div className="at-pos-head">
                <span className="text-dim">{p.symbol} {p.interval}</span>
                <span className={p.direction === 'long' ? 'text-up' : 'text-down'}>
                  {p.direction === 'long' ? '做多' : '做空'}
                </span>
                <span className="text-dim">{p.stateLabel}{p.awaitingEntry ? '（待重挂）' : ''}</span>
                {p.barsHeld != null && <span className="text-dim">第 {p.barsHeld} 根/{p.texitBars ?? '—'} 根退出</span>}
              </div>
              <div className="at-pos-levels">
                <span>入场 {formatPrice(p.avgPrice ?? p.entry)}</span>
                <span className="text-down">止损 {formatPrice(p.stop)}</span>
                {p.target1 != null && <span className="text-up">目标 {formatPrice(p.target1)}</span>}
                {p.beTrigger != null && !p.beDone && <span>保本触发 {formatPrice(p.beTrigger)}</span>}
                {p.trailR != null && p.beDone && <span>跟踪 {p.trailR}R</span>}
                {p.filled != null && p.filled > 0 && <span className="text-dim">数量 {p.filled}{p.beDone ? `（已减 ${p.beQty ?? 0}）` : ''}</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="at-cfg">
        <label>
          单笔风险 %{' '}
          <input
            value={risk}
            onChange={(e) => {
              setRisk(e.target.value)
              setDirty((d) => ({ ...d, risk: true }))
            }}
          />
        </label>
        {st.mode === 'paper' && (
          <label>
            模拟权益 USDT{' '}
            <input
              value={equity}
              onChange={(e) => {
                setEquity(e.target.value)
                setDirty((d) => ({ ...d, equity: true }))
              }}
            />
          </label>
        )}
        <label>
          日亏停机 R{' '}
          <input
            value={dayLimit}
            onChange={(e) => {
              setDayLimit(e.target.value)
              setDirty((d) => ({ ...d, dayLimit: true }))
            }}
          />
        </label>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setDirty({})
            void patch(
              {
                riskPct: Number(risk),
                equityUsd: st.mode === 'paper' ? Number(equity) : undefined,
                dailyLossLimitR: Number(dayLimit),
              },
              '参数',
            )
          }}
        >
          保存参数
        </button>
      </div>

      <details className="at-setup">
        <summary>接入币安账户（测试网先行 → 实盘）</summary>
        <div className="at-setup-body">
          <p className="text-dim">
            测试网（testnet.binancefuture.com 免费注册）验证 2~4 周后再切实盘。API 密钥只开「合约交易」权限，
            绝不开提现。实盘切换需 dryRun=false + testnet=false + confirmLive=true 三重确认。
          </p>
          <label>
            API Key <input value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder={st.keysSet ? st.apiKeyMasked : '...'} />
          </label>
          <label>
            API Secret <input type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} />
          </label>
          <div className="at-setup-actions">
            <button
              type="button"
              disabled={busy || (!apiKey && !apiSecret)}
              onClick={() => void patch({ apiKey: apiKey || undefined, apiSecret: apiSecret || undefined, dryRun: false, testnet: true }, '密钥已保存（测试网）')}
            >
              保存密钥并切测试网
            </button>
            <button type="button" disabled={busy} onClick={() => void onTest()}>
              测试连接
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void patch({ dryRun: true }, '已切回模拟盘')}
            >
              切回模拟盘
            </button>
            <button
              type="button"
              className="at-danger"
              disabled={busy || !st.keysSet || live}
              onClick={() => {
                if (!window.confirm('确认切换到币安主网实盘（真钱）？需再发一次 confirmLive。')) return
                void patch({ dryRun: false, testnet: false, confirmLive: true }, '已切实盘（真钱！）')
              }}
            >
              切实盘（需密钥）
            </button>
          </div>
        </div>
      </details>

      {st.events.length > 0 && (
        <ul className="pos-items at-events">
          {st.events.slice(0, 8).map((ev, i) => (
            <li key={`${ev.at}-${i}`}>
              <span className="text-dim">
                {new Date(ev.at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
              </span>{' '}
              {ev.text}
            </li>
          ))}
        </ul>
      )}
      <div className="text-dim at-note">
        {st.note}；执行器必须单机运行（本机标签 {st.instance}，双机同跑会互相干扰）。
        {' '}平仓原因表：{Object.values(EXIT_LABEL).slice(0, 5).join(' / ')} 等。
      </div>
    </div>
  )
}

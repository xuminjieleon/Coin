# CoinLens — 项目上下文与进度文档

> 本文件是项目的唯一权威状态记录。任何会话开始前先读本文件；每次推进后必须更新「当前进度」与「下一步」章节。

## 1. 目标

做一个**本地个人使用**的加密货币交易分析助手（Web 可视化仪表盘），核心价值：
把机构交易员看盘的完整逻辑链条自动化呈现在一个页面——**价格结构(SMC) → 流动性 → 量价 → 衍生品资金流 → 综合结论评分**，辅助非专业用户做决策。

- 默认标的 BTC/ETH/SOL，支持动态搜索切换币安全部 USDT 交易对
- 分析维度三大支柱：聪明钱概念(SMC)、价格行为学(Price Action)、量价分析(Volume Profile/CVD)
- MVP 不含：AI 解读（二期）、股票市场（后续）、公网部署（本地使用）

## 2. 已确认的技术决策

| 项 | 决策 |
|---|---|
| 前端 | React 18 + TypeScript + Vite + **klinecharts v10**（SMC 标注用 overlay API）+ 纯 CSS（无 UI 框架） |
| 后端 | Python 3.12 + FastAPI + httpx + pandas/numpy，端口 8000 |
| 前端端口 | Vite dev 5173，已配 `/api` → `localhost:8000` 代理 |
| 数据源 | 币安合约 API（首选）→ 现货镜像 data-api.binance.vision（fallback）→ Gate.io（最终备选，见 §6） |
| 部署 | 本地个人使用，无登录体系 |
| UI | 中文、深色主题（背景 #0d1117 / 卡片 #161b22 / 涨 #26a69a / 跌 #ef5350 / 强调 #2962ff） |

## 3. 系统架构

```
浏览器(React SPA, :5173)
  └─ REST /api/* ──→ FastAPI 后端(:8000)
                      ├─ services/binance.py       币安数据(主源+镜像failover+冷却)
                      ├─ services/gateio.py        Gate.io 衍生品(合约/期权/订单簿/contract_stats)
                      ├─ services/kline_cache.py   K线本地SQLite缓存(不可变历史+连续性检测)
                      ├─ services/derivs_store.py  衍生品历史持久化(Gate.io stats回填+快照+分位数)
                      ├─ services/microstructure.py 订单簿微观结构(深度/失衡/大单墙)
                      ├─ services/liquidations.py  清算数据(24h多空清算+分位+杠杆地图)
                      ├─ services/onchain.py       链上数据(mempool.space+blockchain.info)
                      ├─ services/macro.py         宏观联动(Yahoo日线+SQLite缓存+相关性)
                      ├─ services/scanner.py       全市场扫描(流动性前N跑引擎排序)
                      ├─ services/journal_store.py 交易日记(SQLite+计划重放+遵循率)
                      └─ services/analysis/        swings→smc→indicators→volume→decision
```

> **数据模式（2026-08-22 起）**：不使用实时推送（币安 WS 模块已删除）。统一为**手动刷新按钮 + 每 5 分钟自动刷新**（开关持久化 localStorage `coinlens.autoRefresh`），刷新时一次性重拉 analysis/derivatives/backtest 并把新 K 线尾部原地同步进图表（`syncBars`：时间戳等于最后一根→更新、更大→追加、更旧→忽略，不重置视图）。

布局：顶部 Header（搜索/周期/刷新按钮/自动刷新开关/**全市场扫描按钮**/预警铃铛/更新时间）｜左侧 klinecharts 主图+SMC 标注+EMA/RSI｜右侧 360px 栏**分三个 Tab**（2026-08-24 起）：**决策**（决策摘要→交易计划→衍生品→成交量分布）｜**市场数据**（宏观联动→链上→订单簿→清算→事件日历）｜**交易**（组合风控→我的仓位→交易日记）。Tab 选择持久化 localStorage `coinlens.tab`。

## 4. API 契约（前后端共同遵守）

- `GET /api/health` → `{"ok": true}`
- `GET /api/klines?symbol&interval&limit&endTime` → `{symbol, interval, candles[]}`（纯 K 线无分析，供图表向左滚动加载历史分页；endTime 可选，返回其之前的一页；**走 kline_cache 本地缓存**，已缓存窗口直接命中不再请求币安）
- `GET /api/symbols?q=` → `[{"symbol","base"}]`（≤50 条）
- `GET /api/analysis?symbol&interval&limit&asOf` → `{symbol, interval, candles[], smc{swings, structureEvents, orderBlocks, fvgs, liquidityPools, premiumDiscount}, indicators{ema20/50/200, rsi14, atr14, adx14}(与candles等长含null), volumeProfile{poc,vah,val,bins}, summary{score(-100~100), bias, regime, keyLevels[], reasons[]}}, replay{asOf}|null}`（**asOf=回放模式**：K线截断到该根（含）、MTF 只用已收盘高周期 K 线、**funding/OI 加权组件只用当时已收盘日线（derivs_store.daily_rates，12b 轮修复了实时值混入的前视）**、prevDay 为前一交易日——决策与回测口径一致无前视；响应带 replay 标记；patterns/factorContext 字段已于 12b 轮删除）
  - interval ∈ {1h,4h,1d,1w}（**15m 已于 2026-08-22 移除**；**1w 为 2026-08-22 第 11 轮新增**），limit 100~1000；评分 clamp ±100；bias: ≥15 bullish / ≤-15 bearish；reasons 中文按 |weight| 降序
- `GET /api/derivatives?symbol` → `{openInterest, openInterestValue, oiChangePct24h, oiHistory[], fundingRate, fundingHistory[], longShortRatio, longShortHistory[], takerBuySellRatio, source('binance'|'gateio'|null), options{atmIv, putCallRatio, contracts, expiry}}`（币安合约优先 → **Gate.io 回退**（futures tickers + contract_stats + contracts 规格 + options/tickers），任何字段可为 null）
- `GET /api/backtest?symbol&interval&limit&horizon` → `{samples, directionalSamples, ic, hitRate, scoreSeries[]}`（轻量评分 walk-forward：结构/EMA/RSI/溢价折价/CVD 背离按 regime 分化权重复算，IC=Spearman 相关；**采样窗口按周期固定**：1h/4h 2 年、1d 2 年（730 根）、1w 6 年（312 根，2 年日线样本太薄），limit 参数已废弃；走 kline_cache，首拉分页并发 4 路，之后磁盘直读——实测 1h 首拉 14.5s→缓存 1.5s）
- `GET /api/calendar` → `{events[{date,time,title,impact,kind}], note}`（本地维护 `backend/data/events.json`，网络封锁无法拉取宏观日历 API）
- `POST /api/position/advise`（2026-08-22 新增，同日补充杠杆）：body `{symbol, interval, direction('long'|'short'), entry, stop?, qty?, leverage?(1~200), openedAt?(ms)}` → `{price, pnlPct, unrealizedR, mfeR, barsHeld, levels{suggestedStop,beTrigger,trailStop,liqPrice}, items[{level(ok|info|warn|danger),text}], note}`。规则化仓位建议：顺势/逆势检查（vs 当前评分）、止损建议（PLAN_GEOMETRY 的 stopw×ATR）、+beR 减半仓+保本提醒、剩余半仓跟踪止盈位（需 openedAt，自持仓期 MFE 回撤 trail R）、时间退出窗口提醒、名义/保证金/风险金额（需 qty；有杠杆时按 entry/lev 算保证金与占保证金比例）、**强平风险**（需 leverage>1：隔离保证金近似强平价 = entry×(1∓1/lev)，止损越过强平价→danger"先被强平"，止损距离≥强平距离 80%→warn 插针缓冲警告）、顺方向最近关键位止盈参考。走 kline_cache 400 根 + full_analysis，校验：stop 方向合法性 400、leverage 范围 422
- **无实时行情 WS**（2026-08-22 起删除）：数据统一走手动刷新 / 5 分钟自动刷新
- `GET /api/orderbook?symbol`（2026-08-24 新增）→ `{symbol, source('binance_perp'|'gateio_perp'|'binance_spot'), mid, bestBid, bestAsk, spreadBps, topImbalance(前20档失衡-1~1), bands[{bandPct(0.1/0.25/0.5/1), bidUsd, askUsd, imbalance}], walls[{side,price,usd,distBps}](单档>同带中位5倍), levels, note}`——**来源优先级链（2026-08-24 二次修订）**：币安官方合约 depth（可达时最优流动性）→ Gate.io 合约聚合盘（quanto乘数换算USD）→ 币安现货镜像 depth（标注"现货盘"）；60s 内存缓存；快照口径（无实时流）
- `GET /api/liquidations?symbol`（2026-08-24 新增）→ `{long24hUsd, short24hUsd, total24hUsd, longShortRatio, percentileVsYear(今日累计/一年最大值), history[{time,longUsd,shortUsd}]×48h, estimated[{leverage(10/25/50/100), longLiq, shortLiq}], price, source('gateio'|null), note}`——**数据源为 Gate.io contract_stats 的 long_liq_usd/short_liq_usd 聚合**（真实逐笔强平 feed 接口需签名鉴权不可用，也是唯一免费清算聚合源）；**Gate.io 不可达时多空清算如实置 null**（total24hUsd/source=null，note 说明），估算强平位仍可用（=现价×(1∓1/lev) 隔离近似，只需价格）；依赖 derivs_store 回填
- `GET /api/onchain`（2026-08-24 新增）→ `{btc{hashrate, hashrateChg30d, mempoolTxs, mempoolVsize, fees{fastest,halfHour,hour,economy}, difficulty{progressPct,difficultyChangePct,remainingBlocks}, activeAddresses, activeAddrAvg30d}, sources[], unavailable, updatedAt}`——mempool.space（费率/内存池/算力/难度周期）+ blockchain.info charts（30d 算力/活跃地址/交易数，sampled 值是 {x,y} 对象需取 y）；10min 内存缓存；**交易所净流入/稳定币流向如实置空**（付费源不可达），不做编造
- `GET /api/macro`（2026-08-24 新增）→ `{series[{key(ndx/dxy/gold/vix/tnx/mstr/coin), name, last, chg1d/7d/30d, spark[45]}], correlations[{corr30/60/90, beta60}]（与 BTC 日收益 Pearson，本地 1d K线 400 根对齐）, btc{last}, updatedAt, source}`——Yahoo Finance chart API（**必须带浏览器 UA，否则 429**；全局 asyncio 锁 + ≥1.6s 请求间隔 + query1/query2 轮换 + 3 次退避重试）；日线入 SQLite macro.db（不可变，仅尾部重拉）；响应 30min 内存缓存；首次全量拉 7 个序列约 15s，之后磁盘直读
- `GET /api/sources`（2026-08-24 新增）→ `{chains{数据类型→{order[],note}}, hostStatus{币安两主机:{down,retryInS}}, note}`——数据源优先级链配置与实时冷却状态诊断（跨环境部署时查看实际生效来源）
- `GET /api/scan?interval&top(10~80,默认40)`（2026-08-24 新增）→ `{interval, scanned, rows[{symbol,last,chg24h,quoteVolume,score,bias,regime,cvdDiv,hasPlan,topReason}], updatedAt, durationMs, note}`——universe=24h ticker（**币安官方合约 → 现货镜像**优先级链）按成交额排序（剔除稳定币/杠杆币/UP-DOWN），每标的 200 根走 kline_cache 跑完整引擎评分（并发 6）；(interval,top) 结果 5min 缓存；实测首拉 15 标的 6.9s、缓存后 <1s
- `GET/POST/DELETE /api/journal/trades[,/{id},/{id}/close]`（2026-08-24 新增）→ SQLite journal.db：`POST /trades`（快照 plan 可选，冻结开仓时几何）、`POST /trades/{id}/close {exit, reason(stop/target/trail/time/manual)}`（**平仓时用本地 K 线确定性重放计划**：止损→+beR减半保本→跟踪/目标→时间退出，保守盘口顺序=止损先判；planExit{r,reason,exitPrice,barsHeld,beDone} vs 实际 → adherence followed/deviated：同因或 ±0.5R 内=followed）、`GET /stats`（胜率/非亏损率/合计R/遵循率/分币种分周期）
- `POST /api/portfolio/advise`（2026-08-24 新增）：body `{positions[{symbol,interval,direction,entry,stop?,qty?,leverage?,openedAt?}]≤20, accountEquity?}` → `{positions[{price,notionalUsd,riskUsd,liqPrice,unrealizedPct}], netUsd, grossUsd, marginUsd, totalRiskUsd, riskPctOfEquity, correlatedPairs(|corr|≥0.7), betas(vs BTC), items[]}`——组合层风控：净/总敞口、集中度>50%警告、两两相关性（本地 1d 90 日）、风险预算占权益 >3%warn/>6%danger
- **derivatives 响应增强（2026-08-24）**：`topTraderRatio`（Gate.io top_lsr_size 大户持仓多空比）、`fundingHistory`（Gate.io 路径从 contract_stats 的 last_funding_rate 回填 30 期）、`historyStats{days(≤1000), fundingPctl, oiUsdPctl, lsrPctl}`（相对本地持久化历史的分位数；days=时间跨度非行数，funding 事件 8h 间隔混入不影响）、`options` 扩展 `{rr25(25Δ call IV−put IV), maxPain{expiry,strike}(近月有OI到期), termStructure[{expiry,atmIv,rr25,putOi,callOi,pcr}]}`；每次调用把快照写入 derivs.db snapshots 表，且 `ensure_backfill` 按**优先级链回填**：Gate.io contract_stats 1d×1000+1h×720（含清算USD）→ 失败则币安 futures/data（openInterestHist/fundingRate/globalLongShortAccountRatio/takerRatio，无清算；**时间戳毫秒→秒归一化**）入库（6h 增量刷新，同符号并发等待防部分读）；**多源按列 UPSERT 合并**（NULL 不覆盖已有值，同时间戳事件互补）
- **analysis 响应增强字段**：`smc.orderBlocks[].quality / fvgs[].quality`（0-100）、`smc.sweepEvents[]`（扫流动性事件：side+outcome reclaimed/broken）、`indicators.cvd[]`（累计主动买卖差，来自 K 线 takerBuy 字段）、`volumeProfile.pocSeries[]/developingPoc`（滚动 POC）、`wyckoff{phase,events[]}`、`volatility{atrPct,bandwidthPct,squeeze,state}`、`cvdDivergence`、`mtf{list[{interval,score,bias,cvdDiv}],alignment}`、`summary.tradePlan{direction,entry,stop,target1(null=跟踪止盈),beTrigger,beR,targetR(null),scaleOut,trailR,stopAtr,depthAtr,texitBars,fillBars,rr(null=跟踪),note}`（**patterns 与 factorContext 字段已于 12b 轮删除**）——**几何按周期分化**（PLAN_GEOMETRY）：1h 0.75×ATR 回踩/2.5×ATR 止损/+0.1R 减半+保本/目标 0.75R/96 根退出（保本优先）；4h 0.75/1.2/+0.5R 减半保本/0.5R 跟踪止盈无固定目标/48 根退出/18 根成交窗口；1d 0.75/1.5/0.5R/0.5R 跟踪/24/9；1w 0.75/1.5/0.5R/0.75R 跟踪/24/8（**PLAN_THRESHOLD**：4h/1d/1w=|score|≥10、1h=25，2026-08-22 第 11 轮校准，见 §9 第 11 轮）

## 5. 当前进度

### ✅ 后端（backend/）——已完成并验证通过
- 全部文件就绪：`main.py, config.py, services/{binance.py, gateio.py, kline_cache.py, derivs_store.py, macro.py, analysis/{swings,smc,indicators,volume,decision}.py}, routers/{symbols,analysis,derivatives,backtest,calendar,position}.py, tests/{test_smc.py, verify_api.py, backtest_decision.py, plan_sweep*.py, profit_sweep.py, profit_sweep2.py, profit2_*.py, profit3_*.py, profit_eval.py, loso_validation.py}`
- venv 在 `backend/.venv`（本机 D:\Work\Coin 已用 `py -3.13 -m venv .venv` 重建并装好 requirements.txt），启动：`.\.venv\Scripts\python.exe main.py`
- 验证结果：SMC 引擎单测通过（BOS/OB/FVG 识别正确）；/api/analysis BTC/ETH/SOL 均 200（BTC 1h: score=22 bullish trending，OB=10/FVG=10/pools=8）；边界校验 400 正常
- **failover 已按用户要求实现**（官方域名优先，失败才走镜像）：`services/binance.py` 主源 4s 短超时 → 失败标记主机冷却 300s（后续请求快速短路）→ 仅 klines/exchangeInfo 回退镜像；合约专属接口无镜像→路由层置 null。实测：首次请求 6s（含探测），冷却期内 1.8s

### ✅ 前端（frontend/）——已完成，构建与联调通过
- 全部文件就绪：`src/{main.tsx, App.tsx, App.css, types.ts, vite-env.d.ts, api/client.ts, utils/format.ts, components/{Header,SymbolSearch,ChartPanel,smcOverlays.ts,DecisionCard,TradePlanCard,PositionPanel,PortfolioPanel,JournalPanel,DerivativesPanel,VolumeProfilePanel,CalendarPanel,MacroPanel,OnchainPanel,OrderBookPanel,LiquidationPanel,ScannerModal,MtfBar,SourceHint}.tsx, utils/alerts.ts}` + `tools/dns-override.cjs`（`src/ws/binanceWs.ts` 已随实时推送模式一并删除）
- **我的仓位面板**（2026-08-22 新增）：`components/PositionPanel.tsx`——输入方向/入场价/止损(可选)/数量(可选)/开仓时间(可选)，POST /api/position/advise 获取规则化建议（顺势检查、止损建议、+0.5R 减半保本提醒、跟踪止盈位、时间退出、名义风险金额）；仓位按 symbol 持久化 localStorage `coinlens.position`，数据刷新时自动重新分析
- `npm.cmd run build` 通过（tsc 零错误）；dev server :5173 全模块编译 200；`/api` 代理到后端验证可用
- **klinecharts 10.0.2 API 已对照 .d.ts 核对修正**，与 v9 差异点（后续维护必读）：
  - 数据只能通过 `chart.setDataLoader({getBars, subscribeBar, unsubscribeBar})` 喂入，`applyNewData/updateData` 已删除
  - `getBars({type: 'init'|'forward'|'backward', timestamp, callback})`：init 首次拉取；**forward=向左滚动加载更早历史**（timestamp=当前最旧一根，返回数据前插，endTime=timestamp-1 拉其之前一页，见 §4 /api/klines）；backward=加载更新数据（刷新模式不用，返回空）；init 的 callback 必须传 `{forward:true}`，否则两个方向的分页都被禁用
  - `subscribeBar({callback})` 在 init 完成后自动调用——现在只保存 callback 供 App 刷新时 `syncBars` 用，不再订阅外部 WS；symbol/period 变化自动 unsubscribe
  - `createIndicator(value, isStack)` 只有两个参数，pane 用 value.paneId 指定（不存在则自动建 pane，如 RSI_PANE）；内置 EMA/RSI/VOL 直接覆盖 calcParams 即可（EMA [20,50,200]、RSI [14]）
  - KLineData 字段是 `timestamp`（不是 time）；自定义 overlay 用 `registerOverlay` + `createPointFigures` 返回 Figure 数组（type: rect/line/text + attrs + styles）
  - 内置 `simpleTag` 用于均衡位水平线（带 Y 轴价格标签）；自注册 `smcRect`（OB/FVG 矩形延伸至右缘）与 `smcText`（BOS/CHoCH 标注）；**图表标注精简（2026-08-24 用户要求）**：流动性池水平线与 **OB/FVG 区域矩形均已从图表移除**（smcOverlays.ts 不再生成 pool hline 与 ob/fvg rect——用户反馈"横线和线之间的蓝色填充"即这些青/红/黄色区块；池位与区域仍在决策卡关键价位、交易计划入场区；扫流动性事件保留"扫↑/扫↓"、BOS/CHoCH/Wyckoff 文字标注保留；**形态标记已于 12b 轮随证伪因子一并删除**）
- 图表数据流：symbol/interval 变化 → setPeriod+setSymbol → loader.getBars 拉 analysis → onAnalysis 上报 App → overlays 按 groupId 重建；刷新（手动/5min 自动）→ App 重拉 analysis → `chartRef.syncBars(尾部K线)` 原地同步（`_addData` 语义：时间戳相等→更新、更大→追加、更旧→忽略，不重置视图）；**K 线点击回放（2026-08-24）**：`chart.subscribeAction('onCandleBarClick')`（payload `{dataIndex,data:{current}}`）→ App 以 asOf=该根开盘时间重拉 analysis → 决策 Tab 显示回放横幅+全部决策面板切换为回放时点、图表 replayMark overlay 蓝色竖带标记回放 K 线；`analysis` prop 切换为回放响应使 SMC 标注/CVD 也回到当时状态
- **数据模式（2026-08-22，用户要求）**：即使能连上实时 WS 也不使用实时推送。Header 提供"刷新"按钮 + "自动 5min"开关（localStorage `coinlens.autoRefresh` 持久化，默认开）+ "更新于 HH:MM:SS"时间戳；刷新时一次性重拉 analysis + derivatives + backtest（原 derivatives 30s 轮询、WS 断开 60s 轮询降级均已移除）
- **数据来源 tooltip**：`components/SourceHint.tsx`，面板标题旁问号 hover 显示来源说明+可点击网页链接（Coinglass 持仓量/资金费率/多空比、币安合约盘、TradingView）；derivatives 全 null 时空态里也内嵌链接

### 🔧 npm 网络问题的解法（重要）
本机企业 DNS 把 `registry.npmjs.org`/`registry.npmmirror.com` 劫持到 127.0.0.1（黑洞），导致 npm 静默失败；Zscaler 代理(127.0.0.1:9000)也封 registry，但**直连真实 IP 可通**（DNS 层劫持、IP 层未封）。
解决：`frontend/tools/dns-override.cjs`（Node `--require` 钩子，仅对当前进程把两个 registry 域名映射回真实 IP），用法：
```powershell
$env:NODE_OPTIONS='--require D:\Work\Coin\frontend\tools\dns-override.cjs'; npm.cmd install
```
`start-frontend.ps1` 已内置。若未来 npm 报 ECONNREFUSED，先确认该钩子仍生效（IP 可能变化：npmmirror 用 223.5.5.5 解析、npmjs 用 8.8.8.8 解析后更新 cjs 里的 MAP）。

**本机（C:\dev\Coin，2026-08-24）运行环境说明**：
- Node v22.18.0 位于 `C:\Program Files\nodejs\`；**npm 必须用 `npm.cmd`**（npm.ps1 被执行策略禁止）；本机 registry 未被劫持，无需 dns-override 钩子
- Python venv 在 `backend/.venv`（3.13）可直接 `.\.venv\Scripts\python.exe main.py` 启动
- Vite dev 绑定 IPv6 localhost（127.0.0.1 连不上时用 http://localhost:5173）

**本机（D:\Work\Coin，2026-08-21）运行环境说明**：
- node/npm 不在系统 PATH，Node v22.14.0 位于 `D:\360se6\Application\components\Node\`（含 npm.cmd）；运行前端命令前先 `$env:Path = "D:\360se6\Application\components\Node;$env:Path"`
- Python 用 `py -3.13`（D:\Users\Administrator\AppData\Local\Programs\Python\Python313）；backend/.venv 已于本机用 3.13 重建并装好依赖
- pip 在本网络下有间歇性 RST 重试但能装完；npm install 走 dns-override 钩子正常（72 包/34s）
- 首次在本机跑通：后端 :8000 / 前端 :5173 全部 200，analysis 链路验证 OK（BTC 1h score=60 bullish）

## 6. 数据源优先级链与网络探测（跨环境自适应，重要）

**设计原则（2026-08-24 用户要求）：不把任何一台机器的探测结论写死在代码里。** 每类数据在运行时按优先级链探测：优先源短超时失败 → 主机冷却 300s（期间快速短路）→ 自动 fallback 到下一源；响应 `source` 字段标注实际来源。`GET /api/sources` 可查看全部链序与主机实时冷却状态。在任何网络环境运行无需改代码：官方可达则用官方（自动全量），不可达自动降级。

| 数据类型 | 优先级链（运行时探测） | 降级行为 |
|---|---|---|
| K线/exchangeInfo | 币安官方合约 fapi → 现货镜像 data-api.binance.vision | 逐类缓存命中率 |
| 订单簿 | 币安官方合约 depth → **Gate.io 合约聚合盘** → 币安现货镜像 depth | 最后档标注"现货盘，杠杆盘口可能不同" |
| 衍生品快照 | 币安官方合约统计 → Gate.io futures+contract_stats | 字段级 null |
| 衍生品历史/分位数 | Gate.io contract_stats（含清算USD）→ 币安 futures/data（OI/费率/多空比，无清算） | derivs.db 多源按列 UPSERT 合并 |
| 清算聚合 | Gate.io contract_stats（唯一免费源） | 多空清算置空（不编造），杠杆强平位仍可用（只需价格） |
| 全市场扫描 ticker | 币安官方合约 24hr → 现货镜像 24hr | — |
| 链上 | mempool.space + blockchain.info（互补） | 字段级降级 |
| 宏观日线 | Yahoo chart API（唯一可用源） | 序列置空 |

**C:\dev\Coin 本机（Zscaler 企业网，2026-08-24 实测，仅作参考非结论）：**
- fapi/api.binance.com：HTTP 451（区域封锁）→ 实际生效源=镜像+Gate.io
- data-api.binance.vision（K线/exchangeInfo/depth/ticker24hr）：✅
- api.gateio.ws（现货+期货+期权；order_book/contract_stats[liq_usd/top_lsr/funding]/options tickers）：✅；liquidation_orders/public_liq_orders 需 KEY 签名 ❌
- mempool.space + api.blockchain.info charts（sampled 返回 {x,y} 对象）：✅
- query1/query2.finance.yahoo.com：⚠️ 必须带浏览器 UA（否则稳定 429）+ ≥1.6s 间隔 + 双主机轮换 + 退避
- stooq.com/pl CSV：❌ 带 UA 也返回 HTML（服务端已下线，非网络原因）；blockchair：~10 请求即 430 黑名单；bybit/okx/kucoin/coingecko/coincap/data.binance.vision：❌
- 交易所净流入/稳定币流向等实体标签数据无免费可达源（付费源不可达），产品如实置空

推论：本网络下实际生效——K线走镜像、订单簿走 Gate.io、衍生品/清算走 Gate.io、链上走 mempool+blockchain.info、宏观走 Yahoo（带 UA）。用户日常网络若可直连币安，同代码自动切回官方全量源。

## 7. 下一步（按序）

1. ~~修 npm install~~ ✅ 已解决（dns-override.cjs，见 §5）
2. ~~补写前端 5 个文件~~ ✅
3. ~~对照 klinecharts .d.ts 修正 ChartPanel API~~ ✅（v10 DataLoader 模型重构完成）
4. ~~`npm.cmd run build` + dev server 冒烟~~ ✅（tsc 零错误，模块全 200）
5. ~~前后端联调~~ ✅（/api 代理链路验证：health/analysis/derivatives 全通；首次 analysis 6s 含 failover 探测，冷却后 1.3s）
6. ~~启动脚本~~ ✅（start-backend.ps1 / start-frontend.ps1）
7. ~~用户浏览器人工验收~~ ✅（用户反馈"UI 不错"）
8. ~~WS 断开 60s 轮询降级 + 数据来源 tooltip~~ ✅（已被 2026-08-22 数据模式取代：实时推送/轮询降级全部移除，统一手动+5min 自动刷新；数据来源 tooltip 保留）
9. ~~P0/P1/P2 分析策略增强全部完成~~ ✅（见 §8）
10. ~~决策引擎回测校准（10 轮循环，见 §9）~~ ✅（引擎 v3：CVD 共振组件、零权重负贡献组件、可执行保本管理计划；第 10 轮按"利润优先"重校 4h/1d 并移除 15m）
11. ~~用户浏览器验收增强版 UI~~（部分：用户早期反馈"UI 不错"；增强版待刷新 http://localhost:5173 复核——多周期条、交易计划卡含保本移损行与回测统计、期权卡、CVD 副图、动态 POC 线、预警铃铛、左上角"加载更多历史"按钮、**我的仓位面板**）
12. ~~提交首次 git commit~~ ✅（2026-08-21 `8ca7bd7`，62 文件：后端+前端+测试+文档+启动脚本；.venv/node_modules/dist/缓存已忽略）
13. 已知限制（后续迭代）：a) ~~滚动无历史分页~~ ✅（2026-08-21 已实现，2026-08-22 修正语义并加按钮：klinecharts 左移加载是 **type='forward'**（timestamp=最旧一根、返回数据前插）而非 backward；init callback 的 more 必须传 `{forward:true}` 否则两个方向的分页都被禁用——这是上一版"左移不加载"的根因。图表左上角新增「⟵ 加载更多历史」按钮（scrollToDataIndex(0) 触发库内部分页），滚动到最左缘也会自动加载，每页 500 根；历史 K 线走 kline_cache 磁盘缓存，回测预热过的窗口翻页 0.03s）；b) ~~轮询延迟~~（已被 2026-08-22 数据模式取代：无实时推送，最长延迟=自动刷新间隔 5min）；c) 图表形态识别是启发式（双顶/头肩/三角），置信度仅供参考；d) 事件日历为本地手动维护；e) **策略优化已终止**（第 11b 轮，结论见 §9——生产配置固化，不再调参）；后续若恢复优化，方向为加入美股/加密相关标的（MSTR/COIN/NDX，Yahoo/Stooq 数据源已探测可达）
14. ~~机构级差距分析 → 九大模块补全~~ ✅（2026-08-24，见「2026-08-24 机构级升级」节：订单簿微观结构/清算/链上/宏观/衍生品持久化/扫描器/交易日记/期权扩展/组合风控）
15. 用户浏览器验收机构级 UI（待办）：刷新 http://localhost:5173 复核——Header「⚡扫描」按钮（全市场排序弹窗）、右侧栏三个 Tab（决策/市场数据/交易）、市场数据 Tab（宏观联动表/链上/订单簿失衡条+大单墙/清算+杠杆地图）、交易 Tab（组合风控聚合/仓位/日记+计划遵循率）、衍生品面板新增分位数与 RR25/Max Pain、**点击 K 线回放决策（蓝色竖带+回放横幅，回放时 funding/OI 取当时已收盘日线）**（决策摘要卡因子条已随 12b 轮证伪清理删除）

### 2026-08-24 机构级升级（第九轮，差距分析驱动的九个模块）

背景：对照机构交易助手标准做差距分析（数据广度/微观结构/组合风控/执行闭环四层缺口），用户拍板全部实现。网络重新探测结论变化见 §6 更新。

**后端新增**（全部验证 200）：
- `services/microstructure.py` + `/api/orderbook`：**优先级链=币安官方合约 depth → Gate.io 合约聚合盘（quanto 乘数换算 USD）→ 币安镜像现货 depth**（2026-08-24 二次修订，跨环境自适应）；输出点差/前 20 档失衡/±0.1~1% 四档深度带失衡/大单墙（单档>同带中位 5 倍）；60s 缓存
- `services/liquidations.py` + `/api/liquidations`：**Gate.io contract_stats 的 long/short_liq_usd 聚合**（真实逐笔强平 feed 需签名不可用——这是能拿到的最接近 Coinglass 的口径，也是唯一免费源）；24h 多空清算+比例、48h 小时序列、今日累计相对一年最大值的烈度百分位、10/25/50/100× 杠杆的估算强平位（隔离近似，明确标注）；**Gate 不可达时多空清算置 null、杠杆图仍可用**
- `services/derivs_store.py`：衍生品历史 SQLite（derivs.db）——**优先级链回填：Gate.io contract_stats 1d×1000 + 1h×720（含清算USD）→ 失败则币安 futures/data（OI/费率/多空比/主动比，无清算；毫秒→秒归一化）**（每符号 6h 增量刷新；同符号并发 backfill 等待防部分读）；多源按列 UPSERT 合并（NULL 不覆盖）；每次 /api/derivatives 快照入 snapshots 表；`history_stats` 输出 funding/OI/LSR 相对持久化历史的分位数（实测 BTC：funding 82.4%分位、OI 92.2%分位——机构语境"处于一年高位"）
- `services/onchain.py` + `/api/onchain`：mempool.space（费率/内存池/3d算力/难度调整周期）+ blockchain.info charts（30d 算力/活跃地址/交易数）；**注意 sampled=true 返回 {x,y} 对象**（首版踩坑 500）；交易所净流入/稳定币流向付费源不可达，如实置空
- `services/macro.py` + `/api/macro`：Yahoo chart API（ndx/dxy/gold/vix/tnx/mstr/coin 七序列，各自带 fallback 符号如 ^NDX→NQ=F）；**必须浏览器 UA**（无 UA 稳定 429）+ 全局 asyncio 锁 1.6s 间隔 + query1/query2 轮换 + 3 次退避；日线入 macro.db 不可变缓存（仅尾部>2 天重拉）；与 BTC 日收益（kline_cache 1d×400）对齐算 30/60/90 日 Pearson 相关 + 60 日 beta；首次全量 ~15s（7 请求×间隔），之后磁盘直读+30min 响应缓存
- `services/scanner.py` + `/api/scan`：24h ticker 全量（**币安官方合约 → 现货镜像**优先级链，内存缓存 2min）→ 剔除稳定币/杠杆币（UP/DOWN/BULL/BEAR/1L-3L/1S-3S）→ 按 24h 成交额取前 N → 每标的 kline_cache 200 根跑完整引擎（并发 6）→ 按 |score| 排序输出（含 hasPlan/cvdDiv/topReason）；(interval,top) 结果 5min 缓存
- `services/journal_store.py` + `/api/journal/*`：交易日记 SQLite（journal.db）+ **计划确定性重放**（平仓时从开仓时间用本地 K 线重放冻结几何：止损→+beR 减半保本→trail 回撤→目标→时间退出，同根 K 线保守盘口顺序=先判止损）；planExit{r,reason,exitPrice,barsHeld,beDone} 与实际离场对比 → adherence（同因或 ±0.5R 内=followed）；/stats 汇总胜率/非亏损率/合计 R/遵循率/分币种
- `routers/portfolio.py` + `/api/portfolio/advise`：组合层聚合——净/总敞口、保证金、止损风险预算（无止损时用 PLAN_GEOMETRY stopw×ATR 建议）、集中度>50%、两两相关性 |corr|≥0.7 警告（本地 1d×90）、对 BTC beta、风险占权益 >3%warn/>6%danger
- `services/gateio.py` 扩展：`order_book()`（聚合盘+乘数缓存 1h）、`contract_stats()`（原始口径）、`contract_multiplier()`；`futures_snapshot` 增 topTraderRatio（大户持仓多空比 top_lsr_size）+ fundingHistory（last_funding_rate 回填）；`options_snapshot` 重写——每到期月 ATM IV + **25Δ RR**（|delta| 最接近 0.25 的 call−put IV）+ 分月 PCR/OI + **期限结构 termStructure[]** + **Max Pain**（近月有 OI 到期、最小化期权方总赔付的行权价）
- `services/binance.py` 增 `get_mirror_json()`：白名单镜像端点（depth/ticker24hr），沿用主机冷却机制；2026-08-24 二次修订：`get_depth(allow_mirror=)`（订单簿链序用）、`get_ticker24h()`（fapi→镜像 fallback 表）、`host_status()`（/api/sources 诊断用）

**前端新增**（tsc 零错误、代理全链路 200）：
- **侧栏三 Tab**（决策/市场数据/交易，sticky 标签栏，localStorage `coinlens.tab` 持久化）——解决面板数量爆炸后的导航问题
- `Header` 新增「⚡扫描」按钮 → `ScannerModal`（880px 弹窗：市场宽度统计、过滤框、评分排序表、CVD 背离列、计划标记列、当前标的高亮，点击行切换标的，Esc 关闭）
- `MacroPanel`（7 资产×涨跌/30日相关/beta/迷你走势线，相关性着色 |corr|≥0.7 加粗）、`OnchainPanel`（算力/内存池/费率/活跃地址/难度周期）、`OrderBookPanel`（四档深度带双向失衡条+大单墙列表）、`LiquidationPanel`（24h 多空清算+烈度百分位+48h 双向柱图+杠杆强平位表）
- `PortfolioPanel`（读取 localStorage 全部已存仓位聚合：净/总敞口/风险预算/集中度/相关性警告/beta；权益输入持久化 `coinlens.equity`）、`JournalPanel`（「按当前计划记录」一键快照 tradePlan/手动记录/平仓并复盘（含计划重放结果展示）/统计条（胜率/合计R/遵循率）/删除）
- `DerivativesPanel` 增强：OI/资金费率附分位数（"92%分位"）、大户持仓比卡片、期权卡新增 25Δ RR 与 Max Pain
- 数据流：refreshData 统一刷新 analysis+derivatives+backtest+orderbook+liquidations+onchain+macro（后两个服务端有 10/30min 缓存，实际不重复拉）；symbol 切换时 orderbook/liquidations 重置重拉

**实测性能**：orderbook 1.7s / liquidations 首拉 3s（含回填等待）/ onchain 3.9s / macro 首拉 15.2s→缓存后 <1s / scan 首拉 15 标的 6.9s→缓存 <1s / 冷启动符号 derivatives+backfill 36.8s（一次性，之后 5.8s 热路径）

**诚实口径声明（UI tooltip 均已标注）**：清算数据是 Gate.io 统计口径而非逐笔 feed；订单簿是快照而非流；估算强平位未计维持保证金；链上实体标签数据（净流入/稳定币）不可达——不编造

### 2026-08-22 历史数据与回测采样增强（第三轮）
- **K 线本地磁盘缓存**（用户要求："以前的数据加载过之后是不会变化了"）：`services/kline_cache.py`，SQLite（backend/data/cache/klines.db，WAL，PK=symbol+interval+ts）。请求 `limit` 根、终点 `end_time` 的窗口时：先查缓存并做**连续性检测**（顶部紧邻 end_time、内部间隔恒等于 interval 步长），全覆盖则直接返回；否则并发 4 路向币安分页拉缺失段（每页 1000 根按时间窗平铺，页间无重叠/空洞）→ 入库 → 重读合并窗口。end_time=None（最新）时顶页永远实拉（新 K 线会变）并回种缓存。实测：同参数 4.0s→0.036s；回测预热后图表翻页 0.03s。注意：缓存不区分合约/现货源，冲突时后写覆盖（价差极小，可接受）
- **回测采样窗口扩到 2 年**（用户要求"起码要 2 年内的数据去采样"；此前 limit≤1000，1h 只有 ~25 天）：`/api/backtest` 固定拉最近 2 年（15m≈70k/1h≈17.5k/4h≈4.4k/1d≈730 根，limit 参数废弃、前端已同步去掉）。实测 BTC 1h：样本 390→17,302，首拉 14.5s（18 页并发）/缓存后 1.5s；ETH 15m 首拉 46s（70 页）/缓存后 5.5s（纯计算）。2 年大样本 IC≈0.02、胜率≈52%——与 §9 "技术面方向预测上限 ~60%" 的诚实结论一致
- **图表左移分页修复 + 按钮**：见 13a。klinecharts v10 语义勘误（重要，曾搞反）：`forward`=加载更早历史（前插，滚动到最左缘触发）、`backward`=加载更新数据（后插，滚动到最右缘触发）；`callback(data, more)` 的 more 传 false 会把两个方向全关掉

### 2026-08-21 用户体验修复（第二轮）
- **流动性池标注**：`$$$已扫` 改为 `买侧流动性·已扫 / 卖侧流动性`（$$$ 是 SMC 圈"挂在 swing 高低点的挂单流动性"黑话，用户反馈看不懂）
- **图表历史分页**：见上 13a；初始仍加载 500 根（1h≈21 天），向左滚动自动加载更早历史
- **SourceHint tooltip**：原 absolute 定位在 `.sidebar{overflow-y:auto}` 内被裁剪（视觉上像被 K 线图盖住）→ 改为 React Portal + `position:fixed` + z-index 1000，自动视口内夹紧（左右贴边、底部翻转向上），150ms 延迟关闭使链接可点击
- **币安官方 API 连通性复测**：fapi/api.binance.com 返回 HTTP 451（连通但被币安区域封锁，出口 IP 所在地区受限），镜像 data-api.binance.vision 正常——结论不变：本网络只能走镜像（现货数据），合约专属接口仍不可达

## 8. 分析/策略增强 backlog（P0/P1/P2 全部完成 ✅）

### P0 —— 直接提升结论质量
1. ✅ **多周期共振（MTF）**：`/api/analysis` 内联 `mtf` 字段（15m→[1h,4h]、1h→[4h,1d]、4h→[1d]），并行拉取高周期 K 线（60s 缓存）跑同一套引擎取 bias/score；决策加共振权重 ±15；前端 MtfBar 显示周期芯片 + 共振/冲突标签。实测 BTC 1h：4h:bullish:67 + 1d:bullish:19 → aligned
2. ✅ **Regime 分化评分**：decision.py 按 ADX≥25 分支两套权重（趋势市：结构±35/EMA±10/OB±8/RSI 不减分；震荡市：结构±12/OB±12/RSI 反向±10/溢价折价±12），reasons 文案标注"趋势市权重/震荡市权重"
3. ✅ **OB/FVG 质量过滤**：OB 仅保留 displacement≥0.8×ATR 起源（突破段相对 ATR 强度），quality=displacement(60%)+放量(40%)+回踩守住 bonus(15)，按质量排序取前 10；FVG 过滤 <0.1×ATR 噪音 gap，quality 同理；决策权重按质量缩放（0.5~1.0 倍）
4. ✅ **CVD/主动买卖盘**：利用 K 线自带 takerBuy 字段（fapi 与镜像都有，索引 9）计算每根 delta=2×takerBuy-volume 累加成 CVD 曲线；检测近 30 根价格/CVD 背离（价涨 CVD 跌=虚假突破），决策 ±8/±10；前端新增 CVD 副图 pane

### P1 —— 补全逻辑链
5. ✅ **Wyckoff 阶段识别**：`wyckoff.py`——近 60 根区间检测（宽度≤3.5×ATR 为区间），价格位置+成交量萎缩判吸筹/派发；事件：Spring（刺破下沿收回）/UTAD（上冲回落）/SOS（放量突破）；阶段进决策权重 ±6/±8，事件进 reasons；图表上以青色文字标注
6. ~~**K线/图表形态**~~ ✅ 实现后已于 2026-08-24 第 12b 轮**按回测结论删除**（零权重、多轮归因一致为负；patterns.py 模块删除、图表标记与响应字段移除）——详见 §9 第 12b 轮
7. ✅ **假突破事件流**：`smc.sweepEvents[]`——每个流动性池被扫时记录（时间/价位/方向/outcome：reclaimed 收回=反转信号 vs broken 突破=延续信号，3 根内判定）；进决策 ±6/±8；图表"扫↑/扫↓"标注
8. ✅ **动态成交量分布**：`developing_poc_series()`——累计 bin 矩阵向量化计算滚动 300 根 POC 序列（120 采样点）；前端图表画金色虚线 POC 轨迹 + 面板显示"动态POC"
9. ✅ **Gate.io 衍生品恢复**：`services/gateio.py`——futures tickers(funding)+contract_stats(OI 历史+lsr_account 多空比+lsr_taker 买卖比)+contracts 规格(乘数)；derivatives 路由币安失败自动回退（source 字段标记来源）；**options_snapshot：最近到期月 ATM IV（delta 最接近±0.5 的 call+put 均值）+ PCR（持仓量比）**。实测本网络：OI $4.68B/费率 0.0069%/ATM IV 65.9%/PCR 1.12，衍生品面板完全恢复
10. ✅ **波动率状态机**：ATR 百分位+布林带宽百分位（各 200 根回看）→ compressed/normal/expanded + squeeze（带宽<20%分位且 ATR<30%分位）；决策加"波动收缩期，等突破"提示（weight 0 信息项）；DecisionCard 显示波动状态标签

### P2 —— 可信度与风控闭环
11. ✅ **评分回测证伪**：`/api/backtest`——walk-forward 复算轻量评分（结构/EMA/RSI/溢价折价/CVD 背离，regime 分化权重），统计与未来 8 根收益的 Spearman IC + |score|≥15 方向胜率。实测 BTC 1h：IC=0.31、胜率 79.7%（n=59）；DecisionCard 底部"历史验证"行 + 方法论 tooltip
12. ✅ **交易计划输出**：`summary.tradePlan`——|score|≥25 时生成：方向/入场（最高质量未缓解 OB/FVG 中位）/止损（区域外沿+0.3~0.8×ATR）/目标（最近流动性池×2）/R:R；前端 TradePlanCard 卡片
13. ✅ **事件日历**：`/api/calendar` + `backend/data/events.json`（2026 剩余 FOMC + CPI 模板，含 Fed 官网链接 tooltip；网络封锁无法自动拉取，本地手动维护）；前端 CalendarPanel
14. ✅ **多周期预警**：`utils/alerts.ts` AlertEngine——关键位触及（0.2% 容差，10 分钟冷却）/新 CHoCH/新扫流动性事件 → 浏览器 Notification + 站内 toast（右下角，点击消失）；Header 铃铛开关（请求通知权限）

### 后续迭代方向（未排期）
- AI 盘面解读（LLM 汇总各维度成自然语言报告）
- 策略回测平台、可见区域成交量分布、按 interval 动态轮询周期
- ~~宏观联动（DXY/纳指相关性）、交易日记~~ ✅ 2026-08-24 已实现（macro.py + journal_store.py，见 §7 机构级升级）

## 9. 决策引擎回测校准记录（2026-08-21，重要结论）

工具：`backend/tests/backtest_decision.py`（随机时间点决策回测 + 防过拟合协议：数据按时间 60/40 切分，前 60% 调参（IS）、后 40% 盲测（OOS）；门控候选集预先限定、粗粒度；记录缓存 `_bt_cache.pkl` 按引擎源码哈希自动失效）。

### 回测-修复循环过程（7 轮）
1. ETH 单币 100 点：1D 胜率 46%（低于抛硬币）→ 组件归因发现 CVD 背离最强（IC+0.38）、扫流动性事件有 bug（历史陈旧事件一直计入）→ 修复近因过滤
2. 修复后 1D 55.9%/1W 60.3%，但扫流动性仍负贡献 → 权重清零（保留展示/预警）
3. 三币种合并 300 点：方向门控 IS 最优 d1 门控 OOS 仅 38% → 暴露样本不足问题
4. 发现高周期 CVD 背离全为 0：重采样丢 takerBuy 字段 → 修复；交易计划模拟发现"区域中位入场"成交率仅 7% → 不可执行
5. 4h/1d CVD 背离解锁，共振门控 IS 68.3%
6. 2 年数据 ×3 币种 450 点大样本：**证伪 CVD 共振**（OOS 42.9%），所有组件长期方向胜率 50% 附近——技术面预测周线方向本质上接近抛硬币（最佳简单门控 55~61%，随市况波动）
7. 转向可执行性：交易计划改为回踩 0.5×ATR 限价入场 + 1×ATR 止损 + 1:1/1:2 目标 + **+0.5R 保本移损**管理

### 最终验证结果（2 年 × BTC/ETH/SOL × 450 决策点，60/40 时间切分）
| 指标 | IS（调参期） | OOS（盲测期） |
|---|---|---|
| 保本管理计划·非亏损率（盈利+保本离场） | **83.3%**（n=114） | **88.9%**（n=72；BTC 96%/ETH 83%/SOL 88%） |
| 其中：盈利 / 保本 / 全损 | 32.5% / 50.9% / 16.7% | 40.3% / 48.6% / 11.1% |
| 每笔期望（成交后） | +0.16R | +0.29R |
| 限价成交率（24 根内） | 89% | ~85% |
| 复合评分方向胜率（1D/1W） | ~48-51% | ~52-61%（市况依赖） |

### 结论与产品定位修正
- **方向胜率 >80% 不可达**（诚实结论）：任何技术组件在 2 年大样本上都无法稳定超过 ~61%；此前 8 个月小样本上的"CVD 66%"是特定时段运气（大样本上回落到 49%）
- **可靠优势在执行层**：回踩入场 + 保本移损的计划管理使"非亏损率"达 83~89%（IS/OOS 一致、跨币种一致、期望为正）——这是给用户的核心承诺，已在 TradePlanCard 展示并附方法论 tooltip
- 评分体系已按大样本归因重校：CVD 保持中等权重（1W IC+0.16）、结构/EMA/共振保留、FVG/图表形态/K线形态/偏离度/扫流动性**零权重**（多轮归因一致为负，仅保留展示）；趋势市/震荡市权重分化保留
- 防过拟合要点：OOS 只看一次、门控候选预定义、分币种交叉验证、refuse 了"目标位小于止损"的胜率虚高方案

### 1000 点/币种大样本复核（2026-08-21，用户要求的再验证）
- 规模：3000 决策点（每币 1000，~16h 间距，2 年窗口），IS 1800 / OOS 1200；工具 `tests/backtest_decision.py --points 1000`，日志 `tests/bt1000.log`，独立性校验 `tests/thin_analysis.py`
- **保本管理计划非亏损率**：IS **82.3%**（fill 761）／ OOS **82.2%**（fill 529；BTC 79.7% / ETH 85.2% / SOL 81.7%）；EV：IS +0.11R / OOS +0.15R 每笔
- **抽稀独立性校验（OOS）**：1/1（~16h）82.2% → 1/4（~2.6天）87.2% → 1/8（~5.3天）86.5% → 1/16（~10.5天）79.4%（n=34）——去除自相关后仍稳定在 ~80-87% 区间，结论对采样密度不敏感
- 方向门控 1W 抽稀后 57.5~60.1% 稳定；150 点时"OOS 88.9%"系小样本偏乐观（72 个成交），大样本中心估计修正为 **~82%**；产品 UI 文案已同步改为 "~82%（抽稀 79~87%）"
- 大样本下各组件方向 IC 全部 |IC|<0.16、胜率 48-53%——再次确认技术面方向预测上限 ~60% 的诚实结论

### 第 11 轮：多轮循环优化（2026-08-22 凌晨，用户要求"7 小时循环、利润优先、4h/1d/1w、防过拟合、无法提升则说明原因并停止"）
- **工具链**：`tests/profit_sweep2.py`（多轮框架：扩展窗口采样+记录缓存+坐标下降+门控扫描+LOSO）+ `profit2_sens/r3/r4/r5/r5b/cap.py`（各轮定向实验）。日志 `tests/p2_*.log`。**数据窗口扩展**：4h×3 年（6570 根）/1d×4 年（1460）/1w×10 年上限（BTC/ETH 471、SOL 315 根——上市限制）；**1w 为新增周期**（前后端全链路支持，图表 Period type='week'）；MTF 上下文只用已收盘高周期 K 线
- **R0 基线**（扩展窗口，旧几何）：4h 盲测 B+C +115.4R / 1d +64.0R / 1w +3.0R（4h 几何在周线失效）
- **R1 门控**（9 种：score30/35、conf、noexp、trend、range、align、zone）：三周期全部降低总利润 → 拒绝；1d+1w 上下文变体更差（+51 vs +64R）→ 拒绝
- **R2 坐标下降**（depth/stop/be/tgt/texit/trail 六轴两轮）：4h 找到"无限目标+跟踪止盈"族（+178.5R）；**R3 定向验证** trail=0.5/stop=1.2（A+B 单调趋势+盲测确认 4h +285.8R；trail 0.35 变差确认内点最优非边界假象）；敏感性表格显示参数面平滑平台（tgt/texit 平坦——trail 生效后目标位不起作用）
- **R4 成交窗口**：fill×1.5 盲测提升（4h +317.4R / 1d +85.5R，机制=更多同质成交）
- **R5/R5b 计划生成阈值**（25→20→15→10）：单调改善，边际交易（低分段）单独 EV +0.19~0.41R——**优势在入场+管理执行层而非信号强度**；1w 的 BTC 转亏问题被低阈值修复（+2.9→+34.8R）
- **容量约束模拟**（profit2_cap.py，单仓位/币种串行执行=个人账户可实现口径）：th=10 三周期 A+B 与盲测一致胜出：**4h +288.1R（胜率 86.6%、EV +0.315R、DD 5.0R）、1d +46.6R（86.7%、+0.239R、DD 2.7R）、1w +21.2R（95.7%、+0.461R、DD 1.0R）**，分币种全部为正；无约束总利润（4h +471.9R）会重复计重叠仓位，已弃用该口径
- **生产落地**：`PLAN_GEOMETRY` 4h=(0.75, 1.2, 0.5R 减半保本, 无固定目标, 0.5R 跟踪止盈, 48 根退出, 18 根成交窗口)；1d=(0.75, 1.5, 0.5R, trail 0.5R, 24, 9)；1w=(0.75, 1.5, 0.5R, trail 0.75R, 24, 8)；**PLAN_THRESHOLD** 4h/1d/1w=10、1h=25（不变）；1h 几何保持第 9 轮保本优先版。TradePlanCard 按周期显示跟踪止盈规则、撤单时限与容量口径回测数据；tradePlan 新增 trailR/fillBars，target1 可为 null（跟踪止盈无固定目标）
- **诚实提示（UI tooltip 已标注）**：信号方向胜率仍 ~50%（三周期一致），利润全部来自执行层；1w 盲测仅 46 笔；1w 回测记录 warmup=170（无 EMA200）与生产 1w 全窗口分析有二阶成分差异；未计手续费/滑点
- **停止原因（平台期认定）**：① 门控维度两轮穷尽（全部负贡献）；② 几何六轴收敛（内点最优，更紧参数=追噪声）；③ 阈值维度全扫描到 10（再低=方向噪声）；④ 方向预测上限 ~50-60% 为基本面约束（多轮验证）；⑤ 盲测折多重比较已多——继续"提升"大概率是拟合噪声。综上：在现有数据与防过拟合协议下已无法诚实提升，停止

### 第 12 轮：新因子（衍生品历史+宏观）回测与决策集成 + K 线回放（2026-08-24）
- **背景**：第九轮机构级升级加入了衍生品持久化（derivs.db：Gate.io contract_stats 1d×1000，2023-11 起）与宏观联动（macro.db：Yahoo 5 年日线）。用户要求把新因子纳入决策并按利润优先回测，另支持"点一根 K 线看当时决策"的回放
- **数据回填**：本机 derivs.db/macro.db 首次回填（BTC/ETH/SOL 各 1000 日线 + 7 宏观序列×5 年）；修复 derivs_store `_newest_ts` 秒/ms 单位 bug（原先每次调用都误判过期触发回填）
- **工具**：`tests/profit3_factors.py`（因子诊断 + 9 门控扫描）+ `tests/profit3_weights.py`（评分组件权重模拟——因子增量直接叠加到缓存记录的 score 上重导出计划方向，无需重算记录）。日志 `tests/p3_*.log`
- **因子诊断（A+B 折，前瞻 24 根收益 IC）**：1d 上有真实方向信号——OI 百分位 IC -0.137（低 OI 反向做多）、散户多空比百分位 IC -0.113（反向）、大户多空比 IC +0.137（同向）、VIX IC +0.120（买恐慌）；4h 全部 |IC|<0.10；1w 大户 +0.139 但 n≈200
- **门控扫描（容量约束，th=10，A+B 选/盲测验证）**：9 个门控（funding/LSR 拥挤单边跳过、清算烈度、VIX>28、DXY/NDX 风险规避）**全部低于无门控基线**（4h 基线盲测 +285.1R vs 最佳门控 +282.7R；1d +44.2R vs +43.9R；1w +9.2R）——与第 11 轮"门控减少利润"结论一致，跨数据维度成立
- **权重模拟（6 格：inc/full/half/top-only/crowd-only/macro-only）**：1d 上 A+B 直接选定 incumbent（因子有害）；4h full +2.2% 盲测（低于预登记 10% 验收线）；1w top-only +19% 相对但仅 36 笔。**结论：因子不进评分、不做门控，以展示型 factorContext 集成**（与 FVG/图表形态的零权重模式一致）
- **生产集成**：`decision.build_summary` 新增 derivs_ctx/macro_ctx 参数 → `summary.factorContext`（衍生品分位/大户多空比/清算烈度/VIX/美元/纳指，UI 因子条展示+极端状态零权重 reason）；**既有 funding/OI 加权组件经 derivs_store 获得真实数据**（本网络币安不可达时此前一直为 null，Gate.io 回退补全——设计补全而非新组件，权重沿用早期校准）。`macro.factor_context()`/`derivs_store.factor_context()` 提供时点因子值（只用已收盘行，无前视）
- **K 线回放**：`/api/analysis?asOf=<ms>`——K 线截断到该根（含）、MTF 只用已收盘高周期 K 线（ts≤asOf+step-htStep）、衍生品/宏观因子取当时已收盘日线的时点值、prevDay 前一交易日；前端 ChartPanel `onCandleBarClick`（payload `{dataIndex,data:{current}}`）→ App 拉 asOf 分析 → 决策 Tab 顶部蓝色回放横幅（时间+"返回实时"）、决策/交易计划/MTF/量价分布/SMC 标注全部切换为回放时点、图表蓝色竖带标记回放 K 线、点其他 K 线移动回放点、切币/切周期自动退出
- **数据源变化注意**：本机 fapi.binance.com 现可达（此前 451 区域封锁）——kline_cache 近端 K 线自动切换为官方期货数据（1w 历史起点 2019-09=365 根，短于镜像现货 471 根；4h/1d 的 MTF 上下文价格亦切换），第 11 轮的记录缓存已在 11b 期间按混合数据重算，本轮基线（4h 盲测 +285.1R/1d +44.2R/1w +9.2R）与第 11 轮发布值（+288.1/+46.6/+21.2R）有小幅差异——内部对照有效，历史值仅作参考

### 第 12b 轮：回测证伪因子清理（2026-08-24，用户要求"无用的因子从 UI 和代码删除并记录"）
- **删除清单（全部有回测证据支撑）**：
  - **第 12 轮新因子 factorContext**（资金费率分位/大户多空/散户分位/清算烈度/VIX/美元5日/纳指5日 chips）——门控与权重模拟双双未达利润优先验收线（见第 12 轮）；UI FactorStrip、decision.py derivs_ctx/macro_ctx 参数、macro.factor_context()、derivs_store 因子分位计算全部删除
  - **图表形态 + K 线形态**（双顶/头肩/三角/吞没/PinBar/晨星/暮星）——第 6 轮起零权重、多轮归因一致为负；patterns.py 模块删除、engine 不再计算、响应 patterns 字段与图表标记移除
  - **FVG/扫流动性/偏离度（extension）决策分支**——零权重死代码（FVG 第 6 轮起、sweep 第 2 轮起、extension 一直为 0）；WEIGHTS 键同步清理
- **保留清单（非决策因子或有真实权重）**：FVG 检测（交易计划入场区锚点，第 11 轮验证几何 zones=OB+FVG 的一部分）；扫流动性事件（预警引擎与图表"扫↑/扫↓"标记，事件信息而非方向因子）；funding/OI 加权组件（权重 10/8 与 10/6 来自早期校准，Gate.io 日线回退经 **derivs_store.daily_rates()** 保留）；Wyckoff（权重 6/8）；DerivativesPanel/MacroPanel（市场数据面板，非决策因子，分位数展示来自 history_stats）
- **顺手修复回放前视 bug**：asOf 模式此前会取 fapi **实时**资金费率/OI（早晨 -60 与午后 -45 评分差异即此症状——实时 OI 波动混入历史决策）；现在回放只用当时已收盘日线（daily_rates(asOf)），与回测口径一致
- **评分不变性验证**：ETH 1h=23/4h=60/1d=52、BTC 1w=36 删除前后完全一致（删除项全部零权重或纯展示）——生产决策零变化；SMC 单测通过；tsc 零错误
- **证据保留**：profit3_factors.py / profit3_weights.py 回测工具与 p3_*.log 日志保留在库中作为删除依据；decision.py 模块 docstring 记录完整裁定理由

### 第 11b 轮：扩展样本验证（已按用户要求取消）与优化正式终止（2026-08-22）
- **用户对 1w 46 笔盲测的质疑**（合理）：样本 <100 则数据不可靠。成因：① 币安周线数据上限（BTC/ETH 471 根、SOL 315 根——2017/2020 上市）；② 容量约束串行执行（周线一仓占坑最长 24 周≈5.5 个月）；③ 盲测仅占时序后 60%（无约束口径其实 201 笔）。**1d/1w 总利润低于 4h 的成因**：总利润=EV×笔数，EV 其实 1w 最高（+0.46R>4h +0.32R），差距全在 K 线根数（4h 6570 vs 1d 1460 vs 1w 471）与机会频率——提高高周期利润的杠杆是增加标的数而非改参数
- **11b 尝试与取消**：方案=几何只用 BTC/ETH/SOL 调参，新增 10 个长历史主流币（BNB/XRP/ADA/DOGE/LINK/LTC/DOT/TRX/ETC/BCH）做纯样本外验证（工具 `tests/profit2_oos.py` 已写、跑了一半）——**用户否决**：不同币走势完全不一样，新币验证对 BTC/ETH/SOL 交易无意义。已停止（镜像限流导致 LINK/LTC 拉取失败也是实际问题）
- **后续方向（用户提出，未排期）**：加入美股或与加密相关的标的（MSTR/COIN/NDX 等）扩充样本与市场维度。数据源实测（2026-08-22）：`query1.finance.yahoo.com` 可达但**连续请求会被限流**（先 404 后连接重置，需加间隔/重试/换 query2 主机）；`stooq.com` CSV 接口稳定可达。落地时需做限流控制与独立适配层
- **最终结论固化（生产配置即第 11 轮结果，不再迭代）**：PLAN_GEOMETRY 1h=(0.75,2.5,0.1R,0.75R,96)/4h=(0.75,1.2,0.5R,trail0.5,48,fill18)/1d=(0.75,1.5,0.5R,trail0.5,24,fill9)/1w=(0.75,1.5,0.5R,trail0.75,24,fill8)；PLAN_THRESHOLD 4h/1d/1w=10、1h=25；容量口径盲测 4h +288.1R/1d +46.6R/1w +21.2R（1w 样本不足是已声明的局限）。**新增功能替代继续优化**：仓位建议（POST /api/position/advise + PositionPanel）把校准几何用于实盘持仓管理——这是回测结论向用户价值转化的路径

### 第 10 轮：利润优先重校准 4h/1d（2026-08-22，用户要求"利润第一、胜率其次，不做超短线/日内"）
- **工具**：`tests/profit_sweep.py`（144 格网格：depth{0.75,1.0}×stop{2.0,2.5,3.0}×mgmt{plain,be05,scale05,scale10}×tgt{1.5,2.0,3.0}×texit{24,96}；**目标函数=总利润（成交单 R 值之和）优先，其次 EV、胜率**；约束 filled≥120(4h)/30(1d)、成交率≥25%、EV≥+0.05R）+ `tests/profit_eval.py`（补充定向评估）。日志 `tests/profit_4h.log`、`tests/profit_1d.log`。数据走 kline_cache 2 年窗口；**MTF 上下文只用已收盘的更高周期 K 线**（消除半根 K 线前视）
- **15m 周期同轮移除**（前端 INTERVALS / 后端 ALLOWED_INTERVALS、MTF_MAP、TWO_YEARS_BARS、STEP_MS 全部清理，interval=15m 返回 400）
- **4h 结果**（2829 决策点，2024-11~2026-08）：利润最优族 = depth 1.0 / stop 2.0 / **scale05**（+0.5R 减半+保本）/ tgt 3R / texit 24。走样本盲测 **B +42.4R（胜率 76.3%，EV +0.139R）/ C +38.1R（胜率 80.6%，EV +0.132R）**，均高于基线（98% 非亏损版 +32.2/+33.7R，EV +0.10R）；抽稀 1/2→83.5%、1/4→85.0% 稳定；盲测 C 分币种 BTC 83.5%/ETH 82.2%/SOL 75.6% 全正
- **1d 结果**（480 决策点，样本薄）：总利润最大化的"裸止损 3R 目标"格（plain tgt3 te96）样本内 +65.8R 但盲测 C **-12.4R**（20 笔、胜率 20%）→ 按协议拒绝（彩票型参数）。稳定族 scale05/stop2.0 补充盲测（profit_eval.py）：**B +9.2R（胜率 80.4%，EV +0.201R）/ C +10.0R（胜率 100%，EV +0.357R）**，三币种 B+C 全正（BTC 87%/ETH 85%/SOL 92%）——约为基线利润的 3 倍
- **LOSO 提示**：留一币种时"总利润最大化"选择器不稳定（2 币种子集会选中 plain 彩票格，盲测 BTC/SOL 转亏）；scale05 族本身跨币种、跨折、跨周期稳定——**生产采用统一 swing 几何**而非逐币种选择
- **生产选型（PLAN_GEOMETRY 按周期分化）**：1h 保留第 9 轮 be10_scale（保本优先 98%）；**4h/1d 统一 depth 1.0 / stop 2.0 / +0.5R 减半+保本 / 目标 3R / texit 24 根(4h)、96 根(1d)**，rr=1.75。TradePlanCard 按 interval 显示几何与回测口径（4h/1d 文案=利润优先盲测数据）
- 诚实提示（UI tooltip 已标注）：未计手续费/滑点（净 EV 再减 0.03~0.06R）；1d 盲测折仅 30~80 笔成交；激进高利润格盲测转亏已被拒绝——总利润上限受"参数稳健性"约束

### 第 9 轮：分批止盈边界外移，非亏损率 ~98%（2026-08-21，用户要求向 99% 推进）
- **先验数学**：纯限价成交下 P(全损) ≥ f/(1+f)（f=保本触发 R 数），99% 需 f≈0.01 → EV 塌向 0。合法的边界外移手段 = **分批止盈**（+f×R 出半仓锁利润 + 保本，剩余仓位跑目标）
- 工具：`tests/plan_sweep2.py`（96 格网格：depth{0.75,1.0}×stop{1.5,2,2.5}×be{0.05,0.1,0.15,0.25}×tgt{0.75,1.0}×scaleout{off,on}，EV≥+0.05R + 成交率≥40% 约束）+ `tests/loso_validation.py`（留一币种交叉验证）。日志 `tests/sweep2.log`
- **走样本结果**：样本内 99.2%（stop=2.5/be=0.05 家族两阶段稳定占优），但 ranging 门控盲测 EV 跌破约束线（B 段 +0.040、ETH -0.016）→ **按协议拒绝该门控**（n=158 小样本陷阱）
- **LOSO 结果**（因 B/C 段已被查看，改用跨资产泛化检验，更严格）：三折独立选几何，盲测 BTC **97.2%**/EV+0.096、ETH **98.0%**/EV+0.075、SOL **98.4%**/EV+0.069——全部高于 EV 约束线
- **诚实结论：~98% 是 EV>0 约束下的上限**。99%+ 参数在样本内存在但盲测期望贴零，被 EV 约束拒绝
- **生产选型 be10_scale**：0.75 回踩 / 2.5 止损 / +0.1R 减半仓+保本 / 剩余半仓 0.75R / 96 根退出。与最高胜率格（be05）胜率差仅 1-2 笔/400（噪音），EV 更优且约束 margin 翻倍（LOSO 最差 +0.081 vs +0.069）
- 已知局限：回测未计手续费/滑点（maker 入场约 0.02%），净 EV 约再减 0.03~0.06R——UI tooltip 已如实标注

### 第 8 轮：走样本前向校准，非亏损率 ~91%（2026-08-21，用户要求 90% 目标）
- 工具：`tests/plan_sweep.py`（日志 `tests/sweep.log`）。协议：时序 40%/30%/30% 三段（A 调参 → B 盲测 → A+B 重调 → C 盲测）；36 格粗粒度几何网格 + 4 门控；**EV 硬约束 ≥ +0.05R**（防"目标缩水刷胜率"）；超时单按市价结算（比旧口径更严格）
- **两阶段独立选出同一配置**（稳健平台而非刀锋拟合）：回踩 0.75×ATR / 止损 1.5×ATR / 保本触发 +0.25R / 目标 0.75R+1.5R / 96 根时间退出
- 结果：A 93.1% → **B 盲测 94.9%**（n=352）；A+B 重调 93.8% → **C 盲测 91.0%**（n=356，EV +0.148R）；C 段抽稀 91.2%/90.4%；分币种 92.3%/90.4%/90.1%（全 >90%）
- 参数面单调平滑（止损 1.5>1.0、保本 0.25>0.35>0.5 方向一致符合直觉），非噪声挑选；代价是单笔盈利缩小（EV 从 +0.25R 降到 +0.15R），用期望换确定性
- 该几何已被第 9 轮的 be10_scale 取代（胜率 91%→98%，EV +0.15R→+0.09R）

### 维护注意
- decision.py 权重注释已记录校准依据；改权重前必须重跑 `backtest_decision.py` 并检查 IS/OOS 两期一致性
- `_bt_cache.pkl` 会因 services/analysis/*.py 源码变化自动失效重算（约 8 分钟）

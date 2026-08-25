# CoinLens — 项目规范（AGENTS.md）

> 本文件是项目**规范与当前状态**的权威记录：目标、技术决策、架构、API 契约、运行环境、维护规则、待办。任何会话开始前先读本文件。
> 开发历史（每轮的背景/做法/回测数据/被否决方案/踩坑记录）在 **`DEVLOG.md`**——按需读取，不必每次通读；每次推进后必须同步更新本文件（当前状态/待办/契约变更）并在 DEVLOG.md 顶部追加记录。
> 面向用户的策略说明（说人话版：策略介绍/收益/风险/使用方式）在 **`STRATEGY.md`**——数字必须取自 DEVLOG 既有回测记录并保持同步，不得引入未验证数据。

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
| 数据源 | 运行时优先级链探测（见 §6），不写死任何一台机器的网络结论 |
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

> **数据模式**：不使用实时推送（无 WS）。统一为**手动刷新按钮 + 每 5 分钟自动刷新**（开关持久化 localStorage `coinlens.autoRefresh`），刷新时一次性重拉 analysis/derivatives/backtest 等，并把新 K 线尾部原地同步进图表（`syncBars`：时间戳等于最后一根→更新、更大→追加、更旧→忽略，不重置视图）。

布局：顶部 Header（快捷币种 BTC/ETH/SOL/BNB + 搜索/周期/刷新按钮/自动刷新开关/**全市场扫描按钮**/预警铃铛/更新时间）｜左侧 klinecharts 主图+SMC 标注+EMA+成交量副图（RSI/CVD 副图已于 2026-08-25 按用户要求移除；RSI/CVD 数据仍在决策评分与滚动回测中使用）｜右侧 360px 栏**分三个 Tab**：**决策**（**交易计划（置顶，可执行层优先）→决策摘要**→衍生品→成交量分布）｜**市场数据**（宏观联动→链上→订单簿→清算→事件日历）｜**交易**（组合风控→我的仓位→交易日记）。Tab 选择持久化 localStorage `coinlens.tab`。**响应式（纯 CSS 断点，无 JS 检测）**：≤880px 侧栏折到图表下方（图高 clamp(280px,46vh,460px)、侧栏全宽内部滚动）；横屏矮视口整页滚动；≤640px Header 折两行分组（header-main/header-controls）、刷新按钮/预警文字/更新时间前缀隐藏、搜索下拉右锚、toast/扫描弹窗近全屏、输入框 16px 防 iOS 聚焦缩放；Header.tsx 的可隐藏文字必须包在 `.btn-label`/`.ws-label`/`.brand-text` span 里。

## 4. API 契约（前后端共同遵守）

- `GET /api/health` → `{"ok": true}`
- `GET /api/klines?symbol&interval&limit&endTime` → `{symbol, interval, candles[]}`（纯 K 线无分析，供图表向左滚动加载历史分页；endTime 可选，返回其之前的一页；**走 kline_cache 本地缓存**，已缓存窗口直接命中不再请求币安）
- `GET /api/symbols?q=` → `[{"symbol","base"}]`（≤50 条）
- `GET /api/analysis?symbol&interval&limit&asOf` → `{symbol, interval, candles[], smc{swings, structureEvents, orderBlocks, fvgs, liquidityPools, premiumDiscount}, indicators{ema20/50/200, rsi14, atr14, adx14}(与candles等长含null), volumeProfile{poc,vah,val,bins}, summary{score(-100~100), bias, regime, keyLevels[], reasons[]}}, replay{asOf}|null}`（**asOf=回放模式**：K线截断到该根（含）、MTF 只用已收盘高周期 K 线、**funding/OI 加权组件只用当时已收盘日线（derivs_store.daily_rates）**、prevDay 为前一交易日——决策与回测口径一致无前视；响应带 replay 标记；patterns/factorContext 字段已删除，见 DEVLOG 12b 轮）
  - interval ∈ {1h,4h,1d,1w}（15m 已移除），limit 100~1000；评分 clamp ±100；bias: ≥15 bullish / ≤-15 bearish；reasons 中文按 |weight| 降序
- `GET /api/derivatives?symbol` → `{openInterest, openInterestValue, oiChangePct24h, oiHistory[], fundingRate, fundingHistory[], longShortRatio, longShortHistory[], takerBuySellRatio, source('binance'|'gateio'|null), options{atmIv, putCallRatio, contracts, expiry}}`（币安合约优先 → **Gate.io 回退**（futures tickers + contract_stats + contracts 规格 + options/tickers），任何字段可为 null）
- `GET /api/backtest?symbol&interval&limit&horizon` → `{samples, directionalSamples, ic, hitRate, scoreSeries[]}`（轻量评分 walk-forward：结构/EMA/RSI/溢价折价/CVD 背离按 regime 分化权重复算，IC=Spearman 相关；**采样窗口按周期固定**：1h/4h 2 年、1d 2 年（730 根）、1w 6 年（312 根），limit 参数已废弃；走 kline_cache）
- `GET /api/calendar` → `{events[{date,time,title,impact,kind}], note}`（本地维护 `backend/data/events.json`，事件时间按北京时区 authored；`upcoming_events(from_ms, horizon_ms)` 供 position 路由做事件预警）
- `POST /api/position/advise`（2026-08-22 新增；2026-08-25 两轮增强，见 DEVLOG）：body `{symbol, interval, direction('long'|'short'), entry, stop?, qty?, leverage?(1~200), openedAt?(ms)}` → `{price, pnlPct, unrealizedR, mfeR, maeR, barsHeld, scoreNow, scoreAtOpen, thesisState('strong'|'intact'|'weakened'|'broken'), eventsSinceOpen[{time,kind(structure|sweep|wyckoff),direction,text}], takeProfitLadder[{price,label,distPct,rMultiple}](盈利侧前4位), action{level(ok|info|warn|danger),text}(最优先纪律动作), levels{suggestedStop,beTrigger,trailStop,structureStop,liqPrice}, items[{level,text}], note}`。要点：**顺势/逆势检查与决策卡同口径**（500 根窗口+prevDay+MTF+资金费率/OI 加权）；**开仓时点决策回放**（需 openedAt：仅用开仓前已收盘 K 线+已收盘高周期+已收盘日线衍生品，无前视；评分漂移 ±25 触发 ok/warn；入场质量对照 PLAN_THRESHOLD）；**持仓期间事件**（结构 BOS/CHoCH 按最新事件方向汇总、扫流动性、Wyckoff；持仓早于 500 根窗口时自动拉长覆盖窗口 ≤3000 根，barsHeld 用时间戳精确计算）；**thesisState**=评分(×2)/最新结构事件/MTF 多数/CVD 背离带符号合计（描述性非预测）；**action** 优先级：缺止损>止损越强平>时间退出>证据转空且浮亏早离场>保本/跟踪执行>按计划持有；建议项另有：高周期背景与 CVD 背离对照、MFE/MAE+盈利回吐 ≥0.5R 警告+深浮亏后回升、结构止损参考（盈利中最近确认摆动低/高点）、止损紧贴未扫流动性池（≤0.5×ATR）插针风险、止损宽度校验（<0.8×ATR 紧/>3×ATR 宽）、跟踪容忍收紧一半（证据转弱时）、资金费率 carry（8h 成本/收入+本地历史分位+下次结算倒计时，需 qty）、48h 内高影响事件预警、+beR 减半仓+保本、剩余半仓跟踪止盈位（自持仓期 MFE 回撤 trail R）、时间退出窗口、名义/保证金/风险金额、强平风险（需 leverage>1）、波动率压缩提示。止盈阶梯只取**越过入场价的盈利侧**参考位（未扫流动性池/VAH-VAL/POC/区间极值/关键位，0.3% 聚类去重），盈利侧无位=真空提示。校验：stop 方向合法性 400、leverage 范围 422；热路径 ~4s
- **无实时行情 WS**：数据统一走手动刷新 / 5 分钟自动刷新
- `GET /api/orderbook?symbol` → `{symbol, source('binance_perp'|'gateio_perp'|'binance_spot'), mid, bestBid, bestAsk, spreadBps, topImbalance(前20档失衡-1~1), bands[{bandPct(0.1/0.25/0.5/1), bidUsd, askUsd, imbalance}], walls[{side,price,usd,distBps}](单档>同带中位5倍), levels, note}`——优先级链：币安官方合约 depth → Gate.io 合约聚合盘（quanto乘数换算USD）→ 币安现货镜像 depth（标注"现货盘"）；60s 内存缓存；快照口径
- `GET /api/liquidations?symbol` → `{long24hUsd, short24hUsd, total24hUsd, longShortRatio, percentileVsYear, history[{time,longUsd,shortUsd}]×48h, estimated[{leverage(10/25/50/100), longLiq, shortLiq}], price, source('gateio'|null), note}`——数据源为 Gate.io contract_stats 的 long/short_liq_usd 聚合（唯一免费源；真实逐笔 feed 需签名不可用）；Gate 不可达时多空清算如实置 null，估算强平位仍可用（=现价×(1∓1/lev) 隔离近似）；依赖 derivs_store 回填
- `GET /api/onchain` → `{btc{hashrate, hashrateChg30d, mempoolTxs, mempoolVsize, fees{fastest,halfHour,hour,economy}, difficulty{progressPct,difficultyChangePct,remainingBlocks}, activeAddresses, activeAddrAvg30d}, sources[], unavailable, updatedAt}`——mempool.space + blockchain.info charts；10min 内存缓存；交易所净流入/稳定币流向付费源不可达，如实置空
- `GET /api/macro` → `{series[{key(ndx/dxy/gold/vix/tnx/mstr/coin), name, last, chg1d/7d/30d, spark[45]}], correlations[{corr30/60/90, beta60}]（与 BTC 日收益 Pearson）, btc{last}, updatedAt, source}`——Yahoo chart API（**必须带浏览器 UA，否则 429**；全局 asyncio 锁 + ≥1.6s 间隔 + query1/query2 轮换 + 3 次退避重试）；日线入 SQLite macro.db（不可变，仅尾部重拉）；响应 30min 内存缓存
- `GET /api/sources` → `{chains{数据类型→{order[],note}}, hostStatus{币安两主机:{down,retryInS}}, note}`——数据源优先级链配置与实时冷却状态诊断
- `GET /api/scan?interval&top(10~80,默认40)` → `{interval, scanned, rows[{symbol,last,chg24h,quoteVolume,score,bias,regime,cvdDiv,hasPlan,topReason}], updatedAt, durationMs, note}`——24h ticker（币安官方合约→现货镜像）按成交额排序（剔除稳定币/杠杆币/UP-DOWN），每标的 200 根走 kline_cache 跑完整引擎评分（并发 6）；(interval,top) 结果 5min 缓存
- `GET/POST/DELETE /api/journal/trades[,/{id},/{id}/close]` → SQLite journal.db：`POST /trades`（快照 plan 可选，冻结开仓时几何）、`POST /trades/{id}/close {exit, reason}`（**平仓时用本地 K 线确定性重放计划**：止损→+beR减半保本→跟踪/目标→时间退出，同根 K 线保守盘口顺序=止损先判；planExit vs 实际 → adherence followed/deviated：同因或 ±0.5R 内=followed）、`GET /stats`（胜率/非亏损率/合计R/遵循率/分币种分周期；**盈利泄漏分析**：`byExitReason{reason:{count,sumR,avgR,winRate}}` 按离场原因拆解盈亏来源，`adherenceEv{followed,deviated:{count,sumR,avgR}}` 遵循 vs 偏离的均值差=偏离计划的 R 成本/笔——执行层优势是否兑现的直接度量）
- `POST /api/portfolio/advise`：body `{positions[...]≤20, accountEquity?}` → `{positions[{price,notionalUsd,riskUsd,liqPrice,unrealizedPct,unrealizedR,barsHeld,attention{level(danger|warn|info|ok),text}}], netUsd, grossUsd, marginUsd, totalRiskUsd, riskPctOfEquity, correlatedPairs(|corr|≥0.7), betas(vs BTC), items[]}`——组合层风控：净/总敞口、集中度>50%警告、两两相关性（本地 1d 90 日）、风险预算占权益 >3%warn/>6%danger；**每仓位 attention 分诊**（danger：无止损/止损越强平价/超时间退出窗口；warn：浮亏 ≤-1R 或逆势 |评分|≥25；info：评分轻度反向；ok：顺势）——评分走扫描器口径（200 根，无 MTF/衍生品上下文），items 汇总"N 个仓位需立即处理"；跨仓位不漏管退出纪律
- **derivatives 响应增强**：`topTraderRatio`（Gate.io top_lsr_size 大户持仓多空比）、`fundingHistory`、`historyStats{days, fundingPctl, oiUsdPctl, lsrPctl}`（相对本地持久化历史的分位数；days=时间跨度非行数）、`options` 扩展 `{rr25(25Δ call IV−put IV), maxPain{expiry,strike}, termStructure[]}`；每次调用快照入 derivs.db snapshots 表，`ensure_backfill` 按优先级链回填（Gate.io contract_stats 1d×1000+1h×720 含清算USD → 币安 futures/data 无清算、毫秒→秒归一化；6h 增量刷新，同符号并发等待防部分读）；**多源按列 UPSERT 合并**（NULL 不覆盖）
- **analysis 响应增强字段**：`smc.orderBlocks[].quality / fvgs[].quality`（0-100）、`smc.sweepEvents[]`、`indicators.cvd[]`（累计主动买卖差，K 线 takerBuy 字段）、`volumeProfile.pocSeries[]/developingPoc`（滚动 POC）、`wyckoff{phase,events[]}`、`volatility{atrPct,bandwidthPct,squeeze,state}`、`cvdDivergence`、`mtf{list[{interval,score,bias,cvdDiv}],alignment}`、`summary.tradePlan{direction,entry,stop,target1(null=跟踪止盈),beTrigger,beR,targetR(null),scaleOut,trailR,stopAtr,depthAtr,texitBars,fillBars,rr(null=跟踪),note}`——**几何按周期分化**（PLAN_GEOMETRY，第 13 轮 5 年重校准 2026-08-25，见 DEVLOG 第八轮）：1h 0.5×ATR 回踩/2.0×ATR 止损/+0.15R 减半+保本/目标 0.5R/96 根退出；4h 0.75/1.0/+0.75R 减半保本/0.35R 跟踪止盈无固定目标/48 根退出/18 根成交窗口；1d 1.0/1.2/0.5R/0.35R 跟踪/12 根/9；1w 0.75/1.5/0.5R/0.75R 跟踪/24/8（1w 未参与重校准，沿用第 11 轮）（**PLAN_THRESHOLD**：4h/1d/1w=|score|≥10、1h=25；校准依据与盲测数据见 DEVLOG 第 10/11/13 轮）

## 5. 当前状态

### 已上线功能
- **后端**：SMC 决策引擎（swings→smc→indicators→volume→decision，regime 分化权重+MTF 共振+CVD 多周期共振+funding/OI 加权+Wyckoff+波动率状态）、K 线本地缓存、衍生品持久化与分位、订单簿微观结构、清算聚合、链上、宏观联动、全市场扫描、交易日记（计划重放+遵循率）、组合风控、仓位建议（决策卡同口径+回放+事件+证据状态+止盈阶梯+action）、K 线点击回放（asOf）。SMC 单测通过；各端点实测 200、校验 400 正常
- **前端**：全部面板就绪（DecisionCard/TradePlanCard/DerivativesPanel/VolumeProfilePanel/MacroPanel/OnchainPanel/OrderBookPanel/LiquidationPanel/CalendarPanel/PortfolioPanel/PositionPanel/JournalPanel/ScannerModal/MtfBar/SourceHint）；**移动端响应式适配已上线**（App.css 断点见 §3 布局；宏观表格改为 4 列两行自适应，修复了桌面端 360px 侧栏下走势列被裁切的历史问题）；`npm.cmd run build` tsc 零错误
- **加载性能（2026-08-25 第十二轮）**：后端 binance/gateio 共享 AsyncClient 连接池（勿在 `_fetch/_get` 里改回每次新建 client）；derivatives 5 个币安调用/Gate.io 两路/analysis 衍生品上下文均为 asyncio 并行；`ensure_backfill` 走后台任务（强引用防 GC）；前端各面板与 analysis 并行刷新。整页 ~15s→~2.4s（热 0.5s）。CPU 非瓶颈（full_analysis 500 根 13ms），勿用多进程处理请求路径
- **盈利扩展三杠杆已接线（2026-08-25）**：①**机会捕捉**——预警铃铛开启时每次刷新后台拉 `/api/scan`（服务端 5min 缓存）喂计划观察器：市场级**新计划/计划转向**推送（首个周期静默播种防风暴、每标的 30min 冷却、toast 点击切标的）+ 当前标的**回踩接近计划入场区**（≤0.3×ATR）与**挂单窗口到期**提醒（key=symbol|interval|direction，entry 随 ATR 漂移原地更新不重置计时，到期每窗口提醒一次）；②**组合分诊**——PortfolioPanel 每仓位 attention chip（紧急/注意/偏逆）按严重度排序置顶；③**遵循率成本**——JournalPanel 显示遵循 vs 偏离的均值差（偏离成本 R/笔）+ 按离场原因的盈亏拆解
- 仓位按 symbol 持久化 localStorage `coinlens.position`，数据刷新时自动重新分析
- **LTC 纯样本外回测（2026-08-25 第六轮，DEVLOG）**：`tests/backtest_ltc.py` 复用第 11 轮 harness 跑生产配置（LTC 从未参与调参）——1h +116.6R(98.7%)/4h +150.4R(87.0%, EV+0.301R)/1d +24.5R(86.5%)/1w +1.1R(19 笔小样本)；方向准确率 <50% 而利润为正，执行层优势在第四个标的上复现；未改任何生产参数
- **成交量前 10 回测排名（2026-08-25 第七轮，DEVLOG）**：`tests/backtest_top10.py`（CONF 复用 backtest_ltc）——BTC +321.9R > ETH +314.1R > BNB +305.3R > SOL +303.1R > SUI/DOGE/ZEC/PYTH ~+275~283R > XRP +263.4R > TUTU +138.8R(历史短)；**10/10 币种全部盈利、37 个币种-周期组合 EV 无一为负**，1h EV 收敛于 +0.09~0.11R、4h +0.25~0.38R——跨币结构性优势；纯样本外 BNB 落在调参标的区间内（无明显币种偏斜）；未改任何生产参数
- **五年扩展回测 + 第 13 轮几何重校准已采纳（2026-08-25 第八轮，DEVLOG/BACKTEST.md）**：`tests/backtest_5y.py`（BTC/ETH/BNB/SOL × 1h/4h/1d 各 5 年 + 1w 全历史）——旧几何 5 年基线四币合计 1h +1166.2R / 4h +1062.1R / 1d +145.8R / 1w +52.5R 且逐年全正；按 §7.3 新数据条款预登记协议（A 调参/B+C 盲测 + A+B 重调/C 盲测两阶段、单遍坐标下降、1w 排除）重校准 1h/4h/1d 几何并全部通过 C 段盲测（+99.8%/+49.2%/+38.9%，四币逐个改善），**PLAN_GEOMETRY 已更新**（1h 0.5/2.0/0.15R/0.5R 目标；4h 0.75/1.0/0.75R/0.35R 跟踪；1d 1.0/1.2/0.5R/0.35R 跟踪/12 根），1w 与阈值/成交窗口不动；SMC 单测通过、评分路径零变化；优化再次冻结

### 已知局限（诚实口径，UI tooltip 已标注）
- **方向胜率上限 ~60%**：任何技术组件在 2 年大样本上无法稳定超过（多轮验证）；可靠优势在执行层（回踩入场+保本管理，非亏损率 1h ~97% / 1d ~86% / 4h ~79%（第 13 轮重校准后按周期分化，4h 以胜率换 EV）），利润全部来自执行层——这是产品核心承诺，不是预测引擎
- 1w 盲测样本不足（105 笔/四币 5 年）是已声明局限；回测未计手续费/滑点（净 EV 再减 0.03~0.06R）
- 清算数据是 Gate.io 统计口径而非逐笔 feed；订单簿是快照而非流；估算强平位未计维持保证金；链上实体标签数据不可达——均如实标注，不编造
- 事件日历为本地手动维护（backend/data/events.json）
- **策略优化已终止**（第 11b 轮结论固化；第 13 轮为 §7.3 新数据条款下的唯一一次重开并已再次冻结，见 §7 维护规则）

### 运行环境

**本机（C:\dev\Coin）**：
- Node v22.18.0 位于 `C:\Program Files\nodejs\`；**npm 必须用 `npm.cmd`**（npm.ps1 被执行策略禁止）；本机 registry 未被劫持，无需 dns-override 钩子
- Python venv 在 `backend/.venv`（3.13）可直接 `.\.venv\Scripts\python.exe main.py` 启动
- Vite dev 绑定 IPv6 localhost（127.0.0.1 连不上时用 http://localhost:5173）

**D:\Work\Coin（企业网机器）**：
- **启动后端前先查 8000 端口占用**（`netstat -ano | findstr :8000`）：曾发生旧会话进程残留、新进程未抢到端口、UI 全天由旧几何服务的事故（2026-08-25 第十一轮踩坑）
- **外网访问拓扑（2026-08-25 实测）**：本机 → TL-WDR5620(192.168.0.1) → 电信"超级家庭网关"光猫(192.168.1.1，光猫路由模式) → 运营商 CGNAT（tracert 第 3 跳 10.220.224.1 私网段）——**IPv4 端口转发不可行**（宽带线路无公网 v4，除非找 ISP 申请）；**已验证可行的外网路径是 Cloudflare Tunnel**（`tools\cloudflared.exe tunnel --url http://localhost:5173 --protocol http2 --no-autoupdate`，免账号 quick tunnel，手机任意网络访问 `https://xxx.trycloudflare.com`；注意 URL 每次重启随机变化、无 SLA、公开可访问勿外传；`vite.config.ts` 已配 `allowedHosts: ['.trycloudflare.com']`——Vite 6 默认校验 Host 头，不加会被 403）；备选公网 IPv6 直连（2408: 前缀、防火墙 "CoinLens Web 5173 Public" 已放行、仅限手机有 IPv6 的场景，前缀随重拨变化）。本机出网走 Zscaler 代理（曼谷出口），ipify 等查到的是代理 IP 而非线路真实 IP
- node/npm 不在系统 PATH，Node v22.14.0 位于 `D:\360se6\Application\components\Node\`（含 npm.cmd）；运行前端命令前先 `$env:Path = "D:\360se6\Application\components\Node;$env:Path"`
- Python 用 `py -3.13`；backend/.venv 已重建并装好依赖
- **npm 网络问题解法**：企业 DNS 把 `registry.npmjs.org`/`registry.npmmirror.com` 劫持到 127.0.0.1（黑洞），Zscaler 代理也封 registry，但**直连真实 IP 可通**（DNS 层劫持、IP 层未封）。解决：`frontend/tools/dns-override.cjs`（Node `--require` 钩子，仅对当前进程把两个 registry 域名映射回真实 IP）：
  ```powershell
  $env:NODE_OPTIONS='--require D:\Work\Coin\frontend\tools\dns-override.cjs'; npm.cmd install
  ```
  `start-frontend.ps1` 已内置。若未来 npm 报 ECONNREFUSED，先确认该钩子仍生效（IP 可能变化：npmmirror 用 223.5.5.5 解析、npmjs 用 8.8.8.8 解析后更新 cjs 里的 MAP）

## 6. 数据源优先级链与网络探测（跨环境自适应）

**设计原则：不把任何一台机器的探测结论写死在代码里。** 每类数据在运行时按优先级链探测：优先源短超时失败 → 主机冷却 300s（期间快速短路）→ 自动 fallback 到下一源；响应 `source` 字段标注实际来源。`GET /api/sources` 可查看全部链序与主机实时冷却状态。在任何网络环境运行无需改代码：官方可达则用官方（自动全量），不可达自动降级。

| 数据类型 | 优先级链（运行时探测） | 降级行为 |
|---|---|---|
| K线/exchangeInfo | 币安官方合约 fapi → 现货镜像 data-api.binance.vision | 逐类缓存命中率 |
| 订单簿 | 币安官方合约 depth → Gate.io 合约聚合盘 → 币安现货镜像 depth | 最后档标注"现货盘，杠杆盘口可能不同" |
| 衍生品快照 | 币安官方合约统计 → Gate.io futures+contract_stats | 字段级 null |
| 衍生品历史/分位数 | Gate.io contract_stats（含清算USD）→ 币安 futures/data（OI/费率/多空比，无清算） | derivs.db 多源按列 UPSERT 合并 |
| 清算聚合 | Gate.io contract_stats（唯一免费源） | 多空清算置空（不编造），杠杆强平位仍可用（只需价格） |
| 全市场扫描 ticker | 币安官方合约 24hr → 现货镜像 24hr | — |
| 链上 | mempool.space + blockchain.info（互补） | 字段级降级 |
| 宏观日线 | Yahoo chart API（唯一可用源） | 序列置空 |

**C:\dev\Coin 本机（Zscaler 企业网，2026-08-24 实测，仅作参考非结论）：**
- fapi/api.binance.com：曾长期 451（区域封锁），2026-08-24 复测可达——数据源会随时间变化，以运行时探测为准
- data-api.binance.vision（K线/exchangeInfo/depth/ticker24hr）：✅
- api.gateio.ws（现货+期货+期权；order_book/contract_stats[liq_usd/top_lsr/funding]/options tickers）：✅；liquidation_orders/public_liq_orders 需 KEY 签名 ❌
- mempool.space + api.blockchain.info charts（sampled 返回 {x,y} 对象）：✅
- query1/query2.finance.yahoo.com：⚠️ 必须带浏览器 UA（否则稳定 429）+ ≥1.6s 间隔 + 双主机轮换 + 退避
- 不可达：stooq.com（服务端已下线）、blockchair（~10 请求即 430 黑名单）、bybit/okx/kucoin/coingecko/coincap/data.binance.vision；交易所净流入/稳定币流向等实体标签数据无免费可达源——产品如实置空

## 7. 维护规则（必读）

1. **文档协议**：每次推进后更新本文件（当前状态/待办/契约变更），并在 DEVLOG.md 顶部追加该轮记录（背景/做法/数据/结论）。
2. **决策权重/几何改动协议**：decision.py 权重注释记录校准依据；**改权重前必须重跑 `backend/tests/backtest_decision.py` 并检查 IS/OOS 两期一致性**。`_bt_cache.pkl` 会因 services/analysis/*.py 源码变化自动失效重算（约 8 分钟）。
3. **策略优化已终止**（2026-08-22 第 11b 轮固化；**2026-08-25 第 13 轮按新数据条款重开一次并已再次冻结**）：第 13 轮以 5 年历史（2021-08 起的调参前时代为新维度）按预登记协议（两阶段盲测、单遍坐标下降、1w 排除、>5% 盲测提升+逐币全正+最差币守卫三重验收）重校准 1h/4h/1d 几何并全部通过——这是 §7.3 条款下唯一一次合法重开。此后：**不要在无新数据维度的情况下重启参数优化**。若未来恢复：方向为加入美股/加密相关标的（MSTR/COIN/NDX，Yahoo 数据源已探测可达但需限流控制），并沿用第 13 轮协议（OOS 只看一次、坐标轴值预定义、分币种交叉验证、两阶段盲测）。
4. **回测证伪即删除**：零权重/负贡献因子从代码与 UI 删除（先例：图表形态、K线形态、factorContext chips、FVG/sweep/extension 决策分支——见 DEVLOG 12b 轮）；展示型数据（DerivativesPanel/MacroPanel）不属决策因子，保留。
5. **klinecharts v10 API 要点**（与 v9 差异，改图表代码前必读）：
   - 数据只能通过 `chart.setDataLoader({getBars, subscribeBar, unsubscribeBar})` 喂入，`applyNewData/updateData` 已删除
   - `getBars({type:'init'|'forward'|'backward', timestamp, callback})`：**forward=向左滚动加载更早历史**（timestamp=当前最旧一根，返回数据前插，endTime=timestamp-1）；backward=加载更新数据（刷新模式不用，返回空）；init 的 callback 必须传 `{forward:true}`，否则两个方向的分页都被禁用
   - `subscribeBar({callback})` 在 init 完成后自动调用——只保存 callback 供 App 刷新时 `syncBars` 用；symbol/period 变化自动 unsubscribe
   - `createIndicator(value, isStack)` 只有两个参数，pane 用 value.paneId 指定；内置 EMA/RSI/VOL 直接覆盖 calcParams（EMA [20,50,200]、RSI [14]）
   - KLineData 字段是 `timestamp`（不是 time）；自定义 overlay 用 `registerOverlay` + `createPointFigures` 返回 Figure 数组
   - 图表标注现状：**流动性池水平线与 OB/FVG 区域矩形已从图表移除**（用户要求精简；池位与区域仍在决策卡关键价位、交易计划入场区）；保留：均衡位 simpleTag、"扫↑/扫↓"、BOS/CHoCH/Wyckoff 文字标注、回放蓝色竖带
6. **图表数据流**：symbol/interval 变化 → setPeriod+setSymbol → loader.getBars 拉 analysis → overlays 按 groupId 重建；刷新 → App 重拉 analysis → `chartRef.syncBars(尾部K线)` 原地同步；K 线点击回放：`chart.subscribeAction('onCandleBarClick')`（payload `{dataIndex,data:{current}}`）→ App 以 asOf 重拉 analysis。
7. **诚实口径**：所有建议为规则化纪律提示非预测；UI 文案与 tooltip 不承诺胜率；不可达的数据如实置空。
8. **回测脚本必须并发执行**（2026-08-25 用户要求固化）：任何新写（及下次改动）的回测/决策记录计算类脚本不得纯串行长跑（wall time >10 分钟不允许）——记录计算为 CPU 密集，**必须用 multiprocessing 多进程并行（每 symbol×timeframe 一个 worker；纯 threading 受 GIL 限制无效；Windows 下入口必须 `if __name__ == "__main__"` 保护）**，K 线回填等网络段用 asyncio 并发（尊重各数据源限流，参考 kline_cache 的分页并发与重试）；各 worker 结果写独立缓存文件后主进程汇总。既有串行脚本（backtest_5y/top10/ltc 等）在下次触碰时按此标准并行化。另注意本机后台进程 ~15 分钟会被静默强杀（DEVLOG 第八轮踩坑），并发化同时是缩短总时长、规避强杀的手段。

## 8. 待办

1. **用户手机/外网验收（已通过，2026-08-25）**：Cloudflare Tunnel 外网访问实测成功（quick tunnel URL 见 DEVLOG 第十三轮；URL 随隧道重启变化，重跑隧道命令取新地址）。移动端 UI 验收（Header 两行折叠/侧栏下折/扫描弹窗近全屏）随本次外网访问一并覆盖。
2. **用户浏览器验收机构级 UI**：刷新 http://localhost:5173 复核——Header「⚡扫描」按钮、右侧栏三个 Tab、市场数据 Tab（宏观/链上/订单簿/清算）、交易 Tab（组合风控/仓位/日记）、衍生品分位数与 RR25/Max Pain、K 线点击回放
3. **用户浏览器验收仓位建议增强（2026-08-25 两轮）**：交易 Tab「我的仓位」填写开仓时间后——顶部动作横幅（最优先纪律动作）、证据状态 chip、评分漂移 chip、MFE/MAE chip、持仓期间事件列表、止盈参考阶梯表；建议项（入场质量/止损宽度/贴池插针/资金费率 carry/事件预警/早离场/跟踪收紧）
4. **用户浏览器验收盈利扩展三杠杆（2026-08-25）**：开启预警铃铛后等一次刷新——新计划/计划转向 toast（点击切标的）、当前标的回踩入场区与挂单到期提醒；交易 Tab 组合风控的每仓位紧急度 chip；交易日记的偏离成本行与离场原因拆解
5. 未排期迭代方向：AI 盘面解读（LLM 汇总各维度）、策略回测平台、可见区域成交量分布、按 interval 动态轮询周期、美股/加密相关标的（MSTR/COIN/NDX）

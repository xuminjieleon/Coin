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
                      ├─ services/notify.py        PushPlus微信推送通道(markdown+重试)
                      ├─ services/notifier.py      每小时信号推送(整点+5min调度+计划指纹)
                      └─ services/analysis/        swings→smc→indicators→volume→decision
                                                    └─ context.py 决策管线共享层(API与推送同源)
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
- `GET /api/macro` → `{series[{key(ndx/dxy/gold/vix/tnx/mstr/coin), name, last, chg1d/7d/30d, spark[45]}], correlations[{corr30/60/90, beta60}]（与 BTC 日收益 Pearson）, btc{last}, updatedAt, source}`——Yahoo chart API（**必须带浏览器 UA，否则 429**；全局 asyncio 锁 + ≥1.6s 间隔 + query1/query2 轮换 + 3 次退避重试；**403=封锁非限流，`_YahooBlocked` 快速失败不重试**——2026-08-27 起本网络直连 403，路由计划=直连双主机→系统代理双主机（VPN），全路由 403 进 15 分钟封锁窗并服务本地 SQLite 最后缓存日）；日线入 SQLite macro.db（不可变，仅尾部重拉）；响应 30min 内存缓存
- `GET /api/sources` → `{chains{数据类型→{order[],note}}, hostStatus{币安两主机+fapi|sysproxy 代理环节:{down,retryInS}}, systemProxy{url,down,retryInS}, note}`——数据源优先级链配置与实时冷却状态诊断（含 Windows 系统代理探测状态）
- `GET /api/scan?interval&top(10~80,默认40)` → `{interval, scanned, rows[{symbol,last,chg24h,quoteVolume,score,bias,regime,cvdDiv,hasPlan,topReason}], updatedAt, durationMs, note}`——24h ticker（币安官方合约→现货镜像）按成交额排序（剔除稳定币/杠杆币/UP-DOWN），每标的 200 根走 kline_cache 跑完整引擎评分（并发 6）；(interval,top) 结果 5min 缓存
- `GET/POST/DELETE /api/journal/trades[,/{id},/{id}/close]` → SQLite journal.db：`POST /trades`（快照 plan 可选，冻结开仓时几何）、`POST /trades/{id}/close {exit, reason}`（**平仓时用本地 K 线确定性重放计划**：止损→+beR减半保本→跟踪/目标→时间退出，同根 K 线保守盘口顺序=止损先判；planExit vs 实际 → adherence followed/deviated：同因或 ±0.5R 内=followed）、`GET /stats`（胜率/非亏损率/合计R/遵循率/分币种分周期；**盈利泄漏分析**：`byExitReason{reason:{count,sumR,avgR,winRate}}` 按离场原因拆解盈亏来源，`adherenceEv{followed,deviated:{count,sumR,avgR}}` 遵循 vs 偏离的均值差=偏离计划的 R 成本/笔——执行层优势是否兑现的直接度量）
- `POST /api/portfolio/advise`：body `{positions[...]≤20, accountEquity?}` → `{positions[{price,notionalUsd,riskUsd,liqPrice,unrealizedPct,unrealizedR,barsHeld,attention{level(danger|warn|info|ok),text}}], netUsd, grossUsd, marginUsd, totalRiskUsd, riskPctOfEquity, correlatedPairs(|corr|≥0.7), betas(vs BTC), items[]}`——组合层风控：净/总敞口、集中度>50%警告、两两相关性（本地 1d 90 日）、风险预算占权益 >3%warn/>6%danger；**每仓位 attention 分诊**（danger：无止损/止损越强平价/超时间退出窗口；warn：浮亏 ≤-1R 或逆势 |评分|≥25；info：评分轻度反向；ok：顺势）——评分走扫描器口径（200 根，无 MTF/衍生品上下文），items 汇总"N 个仓位需立即处理"；跨仓位不漏管退出纪律
- **derivatives 响应增强**：`topTraderRatio`（Gate.io top_lsr_size 大户持仓多空比）、`fundingHistory`、`historyStats{days, fundingPctl, oiUsdPctl, lsrPctl}`（相对本地持久化历史的分位数；days=时间跨度非行数）、`options` 扩展 `{rr25(25Δ call IV−put IV), maxPain{expiry,strike}, termStructure[]}`；每次调用快照入 derivs.db snapshots 表，`ensure_backfill` 按优先级链回填（Gate.io contract_stats 1d×1000+1h×720 含清算USD → 币安 futures/data 无清算、毫秒→秒归一化；6h 增量刷新，同符号并发等待防部分读）；**多源按列 UPSERT 合并**（NULL 不覆盖）
- **analysis 响应增强字段**：`smc.orderBlocks[].quality / fvgs[].quality`（0-100）、`smc.sweepEvents[]`、`indicators.cvd[]`（累计主动买卖差，K 线 takerBuy 字段）、`volumeProfile.pocSeries[]/developingPoc`（滚动 POC）、`wyckoff{phase,events[]}`、`volatility{atrPct,bandwidthPct,squeeze,state}`、`cvdDivergence`、`mtf{list[{interval,score,bias,cvdDiv}],alignment}`、`summary.tradePlan{direction,entry,stop,target1(null=跟踪止盈),beTrigger,beR,targetR(null),scaleOut,trailR,stopAtr,depthAtr,texitBars,fillBars,rr(null=跟踪),note}`——**几何按周期分化**（PLAN_GEOMETRY，第 13 轮 5 年重校准 2026-08-25，见 DEVLOG 第八轮）：1h 0.5×ATR 回踩/2.0×ATR 止损/+0.15R 减半+保本/目标 0.5R/96 根退出；4h 0.75/1.0/+0.75R 减半保本/0.35R 跟踪止盈无固定目标/48 根退出/18 根成交窗口；1d 1.0/1.2/0.5R/0.35R 跟踪/12 根/9；1w 0.75/1.5/0.5R/0.75R 跟踪/24/8（1w 未参与重校准，沿用第 11 轮）（**PLAN_THRESHOLD**：4h/1d/1w=|score|≥10、1h=25；校准依据与盲测数据见 DEVLOG 第 10/11/13 轮）
- `GET/POST /api/notify[,/test]` → 微信推送（**双通道可切换**：`channel='wecom'`（企业微信群机器人 webhook，免费无限制、免实名，消息收在「企业微信」App，**默认**）｜`'pushplus'`（免费 200 条/天但**需官网实名认证否则 905**，消息收在微信本体））：`GET` → `{enabled, mode('events'|'brief'), channel, symbols[], intervals[](1~4 周期，2026-08-27 起多周期), tokenSet, tokenMasked, wecomKeySet, wecomKeyMasked, lastRun, nextRun, lastError, recent[10], planStates{interval→{symbol→{hasPlan,direction,entry,stop,target1,beTrigger,trailRef(兼容保留,勿用于操作),trailDist,fillBars,score,bias}|null}}}`；`POST /api/notify` body `{enabled?, mode?, channel?, symbols?(1~10), intervals?(1~4 如 ["1h","4h"]；旧版单值 interval 仍接受自动转列表), token?, wecomKey?(完整 webhook URL 或裸 key 均可)}` 更新配置（校验 400；**任何变更重置计划指纹播种**防假事件风暴）；`POST /api/notify/test` 立即按当前通道推测试消息（无凭证 400）。**调度**：整点+5min（等 1h 收盘）对 symbols×intervals 并发跑 `services/analysis/context.run_analysis`（与 /api/analysis 同一份代码；4h 计划状态仅在 4h 收盘后变化，其事件至多每 4 小时一次）；**指纹=symbol|interval→direction**（entry 漂移不重发），持久化 `data/notify.json`（**2026-08-28 第二十七轮起随仓库提交**——用户决定把企业微信 key 进仓库作各机器引导配置，repo 为私有；各机运行后本地状态（指纹/seeded）自行演化，git pull 冲突时保留本地文件；旧单周期配置自动迁移并静默重播种）；events 模式仅推【新】/【转向】/【消失】聚合消息（消息带周期标签如"BTCUSDT 4h 做多"；首轮静默播种），brief 模式每小时全量简报——**两种模式统一【】标题+多行具体价+----------分隔的块状格式**（brief 的观望标的在周期分组下单行列出，2026-08-28 第二十八轮用户定版）；消息只含交易操作的实际价格（入场/止损/止盈/减半保本价；跟踪制周期 4h/1d/1w **直接打印跟踪距离与半仓离场时的具体止损位**——journal 重放口径 stop=max(入场价, 最高价−trailR×风险)，2026-08-28 第二十八轮起，用户无需换算；旧"跟踪启动价"已弃用——它在 4h/1d 上低于实际启用位、有误导），无 R 倍数无评分；分析失败的标的/周期对不参与指纹比对（防假"消失"）；disabled 时仍每小时刷新 planStates 供预览；**入场后仓位管理不推送**（回测口径=开仓时计划冻结，管理位走 App 仓位面板，见 DEVLOG 第十七轮）

## 5. 当前状态

### 已上线功能
- **后端**：SMC 决策引擎（swings→smc→indicators→volume→decision，regime 分化权重+MTF 共振+CVD 多周期共振+funding/OI 加权+Wyckoff+波动率状态）、K 线本地缓存、衍生品持久化与分位、订单簿微观结构、清算聚合、链上、宏观联动、全市场扫描、交易日记（计划重放+遵循率）、组合风控、仓位建议（决策卡同口径+回放+事件+证据状态+止盈阶梯+action）、K 线点击回放（asOf）。SMC 单测通过；各端点实测 200、校验 400 正常
- **前端**：全部面板就绪（DecisionCard/TradePlanCard/DerivativesPanel/VolumeProfilePanel/MacroPanel/OnchainPanel/OrderBookPanel/LiquidationPanel/CalendarPanel/PortfolioPanel/PositionPanel/JournalPanel/ScannerModal/MtfBar/SourceHint）；**移动端响应式适配已上线**（App.css 断点见 §3 布局；宏观表格改为 4 列两行自适应，修复了桌面端 360px 侧栏下走势列被裁切的历史问题）；`npm.cmd run build` tsc 零错误
- **加载性能（2026-08-25 第十二轮）**：后端 binance/gateio 共享 AsyncClient 连接池（勿在 `_fetch/_get` 里改回每次新建 client）；derivatives 5 个币安调用/Gate.io 两路/analysis 衍生品上下文均为 asyncio 并行；`ensure_backfill` 走后台任务（强引用防 GC）；前端各面板与 analysis 并行刷新。整页 ~15s→~2.4s（热 0.5s）。CPU 非瓶颈（full_analysis 500 根 13ms），勿用多进程处理请求路径
- **连接池加固（2026-08-27 第十五轮）**：共享 client 经 `httpx.Limits(keepalive_expiry=60)` 剔除闲置连接（**坑：httpx 0.28 已移除 transport 的 keepalive_expiry 参数，只能走 Limits**）；`_fetch/_get` 遇传输错误（ConnectTimeout 除外——从未连上=真不可达，非僵死连接）用一次性新 client 重试一次、成功则 `_swap_client` 换池——防御长跑进程连接池僵死（08-25 启动的进程跑 1.5 天后对可达主机全部请求失败的事故，重启+此加固解决）；勿改回每请求新建 client，错误路径的一次性重建是防御逻辑
- **系统代理（VPN）接入数据源链（2026-08-27 第十六轮）**：新增 `services/sysproxy.py`——运行时读注册表探测 Windows 系统代理（60s TTL；VPN 客户端通常以系统代理模式工作，httpx 不读 WinINET 设置只认环境变量），传输失败 300s 冷却快速失败，共享代理连接池带同样的 keepalive 僵死防御；**币安链插入"同主机经系统代理"环节**（binance.py `_get`/`get_depth`，独立冷却键 `fapi|sysproxy`——直连失败不阻塞代理尝试、反之亦然），**宏观 Yahoo 路由计划=直连双主机→代理双主机**（403 快速失败保留；直连 403+代理不可用=全路由封锁→15 分钟快速失败窗防每个序列重复撞墙）。代理是**链中一环非全局开关**：VPN 关闭自动回退既有降级链（镜像/Gate），零配置切换。`/api/sources` 新增 `systemProxy` 状态与各链代理环节描述
- **盈利扩展三杠杆已接线（2026-08-25）**：①**机会捕捉**——预警铃铛开启时每次刷新后台拉 `/api/scan`（服务端 5min 缓存）喂计划观察器：市场级**新计划/计划转向**推送（首个周期静默播种防风暴、每标的 30min 冷却、toast 点击切标的）+ 当前标的**回踩接近计划入场区**（≤0.3×ATR）与**挂单窗口到期**提醒（key=symbol|interval|direction，entry 随 ATR 漂移原地更新不重置计时，到期每窗口提醒一次）；②**组合分诊**——PortfolioPanel 每仓位 attention chip（紧急/注意/偏逆）按严重度排序置顶；③**遵循率成本**——JournalPanel 显示遵循 vs 偏离的均值差（偏离成本 R/笔）+ 按离场原因的盈亏拆解
- **每小时微信推送已上线（2026-08-27 第十七轮；2026-08-27 第二十二轮扩展多周期）**：`services/notify.py`（PushPlus/企业微信双通道）+ `services/notifier.py`（整点+5min 调度、计划指纹、events/brief 双模式）+ `routers/notify.py`（配置/状态/test API）；决策管线抽取为 `services/analysis/context.py` 供 API 与推送同源复用（analysis/position 路由已切换，行为零变化，回归通过）；**多周期（第二十二轮）**：配置 `intervals` 列表（1~4 周期，当前 ["1h","4h"]），指纹键 `symbol|interval`，消息带周期标签，planStates 按 interval 嵌套，旧单周期配置自动迁移；消息只含交易操作实际价格；**入场后仓位管理不推送**（口径见 DEVLOG 第十七轮）
- **1h+4h 叠加回测（2026-08-27 第二十二轮，DEVLOG）**：`tests/stacked_mtf.py`（多进程 8 worker；5y 记录缓存已按当前源码哈希重建）——四币 5 年：仅1h +2472.0R/DD4.0R，1h+4h 全额叠加 +3992.6R(+61%)/DD6.9R/并发峰值8仓，**共享预算口径（同币重叠时段各半仓）+3855.8R(+56%)/DD6.8R**——半仓纪律只损失 3.4% 利润；每年叠加均>仅1h（2022 +843.9 vs +502.3R）；同币 1h/4h 仓位重叠率 58~64%、重叠对同向 71~76%；未计手续费。用户结论：叠加划算，风险纪律用共享预算（注：该轮基线系企业网机器数据口径，本机复现第 13 轮权威口径为 1h +2294.4R，机器差 ~7%，见第二十三轮校验）
- **手续费/滑点敏感性 1h vs 4h（2026-08-28 第二十三轮，DEVLOG）**：`tests/fee_compare.py`（口径同 backtest_5y，feeR=双边费率×entry/risk；本机 `_5y_cache_*` 已按当前源码哈希重建可复用）——笔均费用双边 0.10% ≈0.06R（止损距离中位 1.77%/1.84%，两周期绝对距离相当），1h 笔数 4× 而 EV/笔 仅 4h 的 39% → **费用吃掉 1h 边际的 39~47%、4h 仅 14~17%**；四币 5 年总利润：单边 0.05% 下 1h +1393.4R 仍领先 4h +1273.9R，**单边 0.06%（双边 0.117% 交叉点）起 4h 反超**（+1232.3 vs +1213.2R）；maker 入+taker 出（双边 0.07%）1h 仍领先 25%——**1h 存活关键=入场挂单 maker**；1h 非亏损率费后 96.9%→79.7%（双边 0.10%）/71.6%（0.12%），4h 几乎不动；叠加结论对费用鲁棒（双边 0.10% 下仍 +91%）；零生产改动
- **方向拆分 + 2026H1 切片（2026-08-28 第二十五轮，DEVLOG）**：`tests/direction_split.py`（harness 同 fee_compare，每笔保留 direction；1d 缓存已重建为当前哈希，1w 仍过期）——**做多/做空基本对称**：1h 多/空 EV +0.167/+0.164R、4h +0.440/+0.415R（差 <6%），空单占比 57~61% 且总 R 更高纯属笔数；1d 多头略优（+0.452 vs +0.372R）；**三周期两方向逐年全为正**（含牛市年的空单）——执行层优势双向成立；2026H1（UTC 1~6 月）：1h +259.9R 毛/+157.2R 净@0.10%、4h +176.9R/+154.6R（EV +0.519R 超其 5 年均值）、1d +18.2R，合计毛 +454.9R/净 +329.1R，**逐月全正**；**2025 全年**（`--year` 切片）：1h +458.6R/4h +297.7R/1d +55.7R，合计毛 +812.1R/净 +571.5R，12 个月逐月全正、方向对称、分币均衡；未建模资金费率 carry（正费率时空单实盘另有收益）；零生产改动
- **复利口径回测（2026-08-28 第二十九轮，DEVLOG）**：`tests/compound_backtest.py`（harness 同 fee_compare；事件账户=开仓按当时已实现权益快照 f、平仓结算滚入；1h+4h 共享预算叠加复用第二十二轮 scale 规则）——**确认既往全部报告为固定注额非复利（ΣR 线性）**；复利结果是数学上限不可当真：1h f=1% 净@双边0.10% 5 年 154 万×（容量/市场冲击/模型外风险/费率敏感，末期单笔名义额远超市场深度）；**最接近可实现的是 1d：f=1% 净费 5 年 8.1×、年化 +72%、已实现口径 DD 4.0%**（仍未含持仓中浮亏）；实盘介于两口径之间（定期再平衡注额）；复利增长率对费率近线性敏感（1h 毛/净差 4 个数量级）。**追加月化/季化拆解**：194×/年系 CAGR 几何均值非单年成绩（实际分年 ×24~×387、2023 最弱 ×69）；共享预算 f=1% 净月度 60 期 100% 正、中位 +53%/最差 +15%/最好 +127%，季中位 +268%，平滑折算月 ×1.55/季 ×3.73；仅1d 月中位 +5%；单仓名义≈0.54×权益（止损距离中位 1.84%）——**容量在几百万~几千万 USDT 权益量级把复利压回线性区**（1 万起步轨迹：2022 年末 ≈1538 万/单仓 ≈836 万 → 2023 年末 ≈10.6 亿/单仓 ≈5.8 亿，机器输出），"100% 月份为正"本身即模型过乐观信号。零生产改动；本轮把 1h/4h/1d 记录缓存统一重存为 backtest_5y 的 window 键（stacked_mtf 旧键缺 window 不兼容的坑已修，`load_records` 已统一）
- 仓位按 symbol 持久化 localStorage `coinlens.position`，数据刷新时自动重新分析
- **LTC 纯样本外回测（2026-08-25 第六轮，DEVLOG）**：`tests/backtest_ltc.py` 复用第 11 轮 harness 跑生产配置（LTC 从未参与调参）——1h +116.6R(98.7%)/4h +150.4R(87.0%, EV+0.301R)/1d +24.5R(86.5%)/1w +1.1R(19 笔小样本)；方向准确率 <50% 而利润为正，执行层优势在第四个标的上复现；未改任何生产参数
- **用户 Pine 脚本对比回测（2026-08-26 第十四轮，DEVLOG）**：`tests/backtest_pine.py`（每 symbol 一 worker 并发）忠实复刻用户三个 TradingView Pine 脚本并与生产 1h/4h 策略在同一 5 年窗口/同一费率下对比——生产策略 MAR 28~71（1h）/15~25（4h）vs Pine 0.8~2.6、回撤 3~4%（1h）/5~6%（4h）vs 21~38%、逐年全正（Pine 单笔 EV 更高但笔数少 17 倍且 BTC 有亏损年）；4h 单笔净 EV +0.36~0.41R 约为 1h 的 3 倍但权益回撤更大、非亏损率更低——1h 曲线更稳、4h 执行可行性更高；CoinLens 侧直接复用 `_5y_cache_*` 记录缓存零重算；无生产代码改动
- **成交量前 10 回测排名（2026-08-25 第七轮，DEVLOG）**：`tests/backtest_top10.py`（CONF 复用 backtest_ltc）——BTC +321.9R > ETH +314.1R > BNB +305.3R > SOL +303.1R > SUI/DOGE/ZEC/PYTH ~+275~283R > XRP +263.4R > TUTU +138.8R(历史短)；**10/10 币种全部盈利、37 个币种-周期组合 EV 无一为负**，1h EV 收敛于 +0.09~0.11R、4h +0.25~0.38R——跨币结构性优势；纯样本外 BNB 落在调参标的区间内（无明显币种偏斜）；未改任何生产参数
- **五年扩展回测 + 第 13 轮几何重校准已采纳（2026-08-25 第八轮，DEVLOG/BACKTEST.md）**：`tests/backtest_5y.py`（BTC/ETH/BNB/SOL × 1h/4h/1d 各 5 年 + 1w 全历史）——旧几何 5 年基线四币合计 1h +1166.2R / 4h +1062.1R / 1d +145.8R / 1w +52.5R 且逐年全正；按 §7.3 新数据条款预登记协议（A 调参/B+C 盲测 + A+B 重调/C 盲测两阶段、单遍坐标下降、1w 排除）重校准 1h/4h/1d 几何并全部通过 C 段盲测（+99.8%/+49.2%/+38.9%，四币逐个改善），**PLAN_GEOMETRY 已更新**（1h 0.5/2.0/0.15R/0.5R 目标；4h 0.75/1.0/0.75R/0.35R 跟踪；1d 1.0/1.2/0.5R/0.35R 跟踪/12 根），1w 与阈值/成交窗口不动；SMC 单测通过、评分路径零变化；优化再次冻结
- **A股 ETF 可行性回测（2026-08-27 第十九轮，DEVLOG）**：`tests/ashare_data.py`（东财 push2his 日线前复权，12 只 ETF 本地 pickle 缓存，**试验层未接生产**）+ `tests/backtest_ashare.py`（生产 1d 引擎原封不动，A 股执行口径：long-only/T+1 管理自次日/T+0 品种当日止损/跳空按开盘成交/双边 0.06% 费；预登记三关写死于 docstring）——12 ETF（50/300/500/1000/科创50/创业板/证券/半导体/红利/纳指/黄金/恒生互联网）净 +143.2R（1714 笔、胜率 66.2%、EV +0.084R）、8/12 盈利、最差 EV −0.076R，**G1/G2/G3 三关全过 → 可行**；结构性结论：盈利集中大盘蓝筹+T+0 品种，高波动主题/小盘（1000/半导体/证券/科创50）亏损，2018 −31.7R 为最大亏损年；方向准确率 48~58% 与加密一致（执行层优势跨市场复现）；**未调任何参数，未接生产引擎**；顺带修复 `services/analysis/volume.py` first-bin 越界潜伏 bug（A股平线触发，币安数据从未遇到）
- **A股预登记调参轮（2026-08-27 第二十轮，DEVLOG）**：`tests/tune_ashare.py`（A/B/C 时间折两阶段盲测；轴 volgate/bullgate/th/stop/be/texit）——**候选（stop1.5/be0.75）被 K3 最差成员守卫一票否决**（C 段池化 +52%、2023 −11.8→−3.3R 均兑现，但红利ETF −7.6 vs −6.0R 恶化）；波动率分闸（volgate=noexp）与 EMA200 牛市闸两阶段均被数据拒绝（expanded 波动期含大量盈利交易、闸门切掉盈亏两侧）；2018（调参段内）无法在不伤总利润前提下收敛；**最终 A 股配置=第十九轮生产基线原封不动**，逐年/逐 ETF 收益表格已产出供产品化决策；若未来放弃最差成员守卫须重新预登记另开一轮
- **A股独立策略研究·T+0 扩池（2026-08-27 第二十一轮，DEVLOG）**：抛开现有策略的独立研究。定调：现实对标=币圈 1d 家族（~29R/年）；T+0 三只 EV +0.165R vs T+1 +0.060R（2.7×）为核心线索。`tests/t0_overnight.py` 预登记协议执行完毕：9 只 T+0 池（美股 3/黄金 2/港股 3/日本 1）**盲测 H1/H2/H3 三关全过 → 扩池方向成立**——盲段 2.7 年 370 笔、胜率 68.9%、EV +0.161R、净 +59.6R（**年化 22.3R/年**，含 2025 黄金大年 +50.3R 繁荣成分；全时段口径 12.3R/年）、DD 9.3R、2018 仅 −5.8R（远浅于 12 ETF 池的 −31.7R）；新 6 只贡献 +96.2R > 旧 3 只 +63.2R（与第十九轮口径自洽）；**隔夜因子无证据**（NDX/GOLD 闸门覆盖迟于 A 段致选择不可区分+敏感性 −0.5%/−1.5% 方向不一，因子无害不采纳）；唯一弱成员=159920 恒生ETF（盲段 EV −0.123R）。数据层：60m 死于东财只有 128 根；ETF 日线=东财→腾讯 ifzq.gtimg 备胎自动切换（end 须横杠日期）；NDX/GOLD 驱动走本地 macro.db；东财 burst 探测触发 IP 封禁须单发慢速。配置=生产 1d 引擎原封不动；未接生产

### 已知局限（诚实口径，UI tooltip 已标注）
- **方向胜率上限 ~60%**：任何技术组件在 2 年大样本上无法稳定超过（多轮验证）；可靠优势在执行层（回踩入场+保本管理，非亏损率 1h ~97% / 1d ~86% / 4h ~79%（第 13 轮重校准后按周期分化，4h 以胜率换 EV）），利润全部来自执行层——这是产品核心承诺，不是预测引擎
- 1w 盲测样本不足（105 笔/四币 5 年）是已声明局限；回测主口径未计手续费/滑点——实测折扣（第二十三轮）：双边 0.10% ≈0.06R/笔（止损中位 ~1.8%），1h EV −39%/4h −14%，总利润交叉点双边 0.117%（单边 0.059%）；1h 的 ~97% 非亏损率是毛口径，费后 ~80%（双边 0.10%）
- **1h 回测利润为上界（2026-08-28 第三十轮审计，用户裁定按下界呈现）**：回测执行层 `sim_outcome_fast` 同根 K 线先判目标、后判保本触发；计划语义与日记重放（journal_store）是 +beR 保本挂单先于目标成交。1h（唯一固定小目标周期）成交根一根 K 线贯穿保本线+目标线时多算 +0.175R/笔——五年 22% 的 1h 交易受影响、合计多算 +543R。**1h 下界口径（日记语义）：毛 EV +0.125R / 总利润 +1770R（vs 上界 +0.164R / +2313R）**，下界仍逐年全正且优于旧几何；下界口径下 1h 在双边 0.10% 起即被 4h 反超（原口径反超点 ~0.12%），maker 入场对 1h 更关键。**4h/1d/1w 为跟踪族两顺序等价（实测差异精确为 0），其固定注额数字不受影响；BACKTEST §5 复利节的 1h 相关行已改按下界重算**（仅1h 净 f=1% 期末 154 万×→6884×、年化 ×6.0/年；共享预算 1890 亿×→8.68 亿×、年化 65×/年——下界把 1h 复利压掉 2~4 个数量级，且使容量墙从 2023 推迟到 2024 年）。BACKTEST.md 文首 + §4.3 附表 + §4.4 附注 + §5、STRATEGY.md §4 已按此修订
- **Wyckoff spring/utad 是死代码（第三十轮审计发现，冻结未改）**：`wyckoff.py:11-12` 区间极值含被扫描的最近 15 根自身，`wyckoff.py:32-36` spring/utad 判定恒为假——该加减分分支（decision.py:318-323）从未执行。全部历史校准在此行为下完成、与当前权重自洽，**不能静默修**（修复=改评分构成，须按 §7 协议重走验证）；当前按"该分支不存在"的口径接受现状
- **回测 vs 生产的口径差（第三十轮审计定位+定量，无前视违规）**：回测记录不含 funding/OI 组件（生产实盘含，权重最高 ±20，第 12 轮已证伪其增量；derivs 历史仅 ~2.7 年故 5 年本就无法模拟）；prevDay 磁吸组件回测无/生产有（**实证 0/24 样本受影响**——PDH/PDL 池 touches=1 恒被 `pools[:8]` 截断中和）；MTF 边界一根高周期 bar 差（机制存在，但回测 spacing=4 网格相位=0 使其对回测记录覆盖率 0%）。引擎核心经审计无未来函数、指标逐值 0 偏差、决策层记录 vs 生产重算 45/45 一致、执行层 clean-room 重放 4349/4349 一致
- **工程隐患（第三十轮登记）**：`source_hash()` 只覆盖 `services/analysis/*.py`，不含 harness 参数（warmup/spacing/min_bars/fill_mult）——改这些参数不会令记录缓存失效，须手工 `--refresh`；建议后续纳入键
- 清算数据是 Gate.io 统计口径而非逐笔 feed；订单簿是快照而非流；估算强平位未计维持保证金；链上实体标签数据不可达——均如实标注，不编造
- 事件日历为本地手动维护（backend/data/events.json）
- **宏观源 Yahoo 2026-08-27 起直连 403**（curl/httpx 双通道、带 UA 均拒；VPN 系统代理下可达）：无 VPN 时宏观序列冻结在最后缓存日、macro.py 403 快速失败（15 分钟封锁窗），VPN 开启时自动经代理续更；策略评分不含宏观数据，不受影响
- **策略优化已终止**（第 11b 轮结论固化；第 13 轮为 §7.3 新数据条款下的唯一一次重开并已再次冻结，见 §7 维护规则）

### 运行环境

**本机（C:\dev\Coin）**：
- Node v22.18.0 位于 `C:\Program Files\nodejs\`；**npm 必须用 `npm.cmd`**（npm.ps1 被执行策略禁止）；本机 registry 未被劫持，无需 dns-override 钩子
- Python venv 在 `backend/.venv`（3.13）可直接 `.\.venv\Scripts\python.exe main.py` 启动
- Vite dev 绑定 IPv6 localhost（127.0.0.1 连不上时用 http://localhost:5173）

**D:\Work\Coin（企业网机器）**：
- **启动后端前先查 8000 端口占用**（`netstat -ano | findstr :8000`）：曾发生旧会话进程残留、新进程未抢到端口、UI 全天由旧几何服务的事故（2026-08-25 第十一轮踩坑）。后台方式启动：`Start-Process -FilePath "D:\Work\Coin\backend\.venv\Scripts\python.exe" -ArgumentList "main.py" -WorkingDirectory "D:\Work\Coin\backend" -WindowStyle Hidden -RedirectStandardError "D:\Work\Coin\backend\data\uvicorn-8000.log"`（注意 venv launcher 会带一个同名子进程，杀进程时父子都杀）
- **网络实测（2026-08-27，会随运营商策略变化，一切以运行时探测为准）**：fapi.binance.com **被 DNS 污染**（解析到假 IP 轮换：104.244.43.231/69.63.186.30/2001::…，TCP 超时；08-24 时还可达）——K线/订单簿/衍生品/ticker 自动降级镜像+Gate.io，实测降级链全通（orderbook→gateio_perp、derivatives→gateio、analysis 走镜像 K线）；data-api.binance.vision / api.gateio.ws（全端点）/ mempool.space / api.blockchain.info 均 ✅；Yahoo query1/2 一律 403（宏观冻结，见已知局限）。币安合约独有端点（premiumIndex、futures/data 系列）污染期间无镜像，衍生品数据由 Gate contract_stats 等价覆盖、无实质缺失
- **VPN（系统代理模式）实测（2026-08-27）**：用户 VPN 客户端以系统代理工作（注册表 ProxyEnable=1 + ProxyServer=127.0.0.1:13059，非 TUN 全局接管——curl/python 直连不走它，需显式 -x/代码支持）。经代理：**fapi 全端点（含 premiumIndex/futures/data）+ Yahoo 全部 200**（出口 IP 104.234.240.117）。后端已按第十六轮改造自动探测并利用系统代理：VPN 开启时 derivatives 恢复 binance 主源（真实 OI ~$8.3B vs Gate ~$4.6B）、宏观自动续更；VPN 关闭自动回退降级链，无需任何配置
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
| K线/exchangeInfo | 币安官方合约 fapi（直连→系统代理）→ 现货镜像 data-api.binance.vision | 逐类缓存命中率 |
| 订单簿 | 币安官方合约 depth（直连→系统代理）→ Gate.io 合约聚合盘 → 币安现货镜像 depth | 最后档标注"现货盘，杠杆盘口可能不同" |
| 衍生品快照 | 币安官方合约统计（直连→系统代理）→ Gate.io futures+contract_stats | 字段级 null |
| 衍生品历史/分位数 | Gate.io contract_stats（含清算USD）→ 币安 futures/data（OI/费率/多空比，无清算） | derivs.db 多源按列 UPSERT 合并 |
| 清算聚合 | Gate.io contract_stats（唯一免费源） | 多空清算置空（不编造），杠杆强平位仍可用（只需价格） |
| 全市场扫描 ticker | 币安官方合约 24hr（直连→系统代理）→ 现货镜像 24hr | — |
| 链上 | mempool.space + blockchain.info（互补） | 字段级降级 |
| 宏观日线 | Yahoo chart API（直连→系统代理） | 序列冻结在最后缓存日 |
| A股 ETF 日线（**试验层，未接生产**） | 东财 push2his → 腾讯 ifzq.gtimg fqkline 备胎（`tests/t0_overnight.py` 已接自动切换） | 本地缓存；无 takerBuy → CVD 组件中性降级；60m 历史只有 ~128 根不可回测；**burst 快速请求触发东财 IP 级临时封禁（直连/代理同灭），抓取须单发慢速** |

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
9. **报告叙述性数字必须机器核验**（2026-08-29 第二十九轮勘误教训固化）：BACKTEST.md/STRATEGY.md/DEVLOG 中的**叙述性推导数字**（排名、单位换算、权益轨迹等非脚本输出直抄值）必须由脚本打印输出或逐项对照运行日志核验后写入，**不得心算**——助手生成的叙述文字没有执行校验，是本项目的错误高发层（先例：第二十九轮排名错位一处 + 10× 换算滑位两处，均写入两份文档，由用户质疑触发机器审计才发现；而同期代码层 bug（变量遮蔽/同 bar 进出排序）全部被运行时当场捕获，无一流入表格数字）。回测脚本应把报告会用到的叙述数字（轨迹/排名等）直接打印成输出段（先例：`compound_backtest.py` 权益轨迹与年化排名段），文档只准抄脚本输出。表格直抄值与叙述推导值的可信度是两个层级，用户质疑任何一个数字时先跑机器审计再回答。

## 8. 待办

1. **用户手机/外网验收（已通过，2026-08-25）**：Cloudflare Tunnel 外网访问实测成功（quick tunnel URL 见 DEVLOG 第十三轮；URL 随隧道重启变化，重跑隧道命令取新地址）。移动端 UI 验收（Header 两行折叠/侧栏下折/扫描弹窗近全屏）随本次外网访问一并覆盖。
2. **用户浏览器验收机构级 UI**：刷新 http://localhost:5173 复核——Header「⚡扫描」按钮、右侧栏三个 Tab、市场数据 Tab（宏观/链上/订单簿/清算）、交易 Tab（组合风控/仓位/日记）、衍生品分位数与 RR25/Max Pain、K 线点击回放
3. **用户浏览器验收仓位建议增强（2026-08-25 两轮）**：交易 Tab「我的仓位」填写开仓时间后——顶部动作横幅（最优先纪律动作）、证据状态 chip、评分漂移 chip、MFE/MAE chip、持仓期间事件列表、止盈参考阶梯表；建议项（入场质量/止损宽度/贴池插针/资金费率 carry/事件预警/早离场/跟踪收紧）
4. **用户浏览器验收盈利扩展三杠杆（2026-08-25）**：开启预警铃铛后等一次刷新——新计划/计划转向 toast（点击切标的）、当前标的回踩入场区与挂单到期提醒；交易 Tab 组合风控的每仓位紧急度 chip；交易日记的偏离成本行与离场原因拆解
5. **用户验收微信推送（2026-08-27 第十七轮）**：pushplus.plus 扫码登录复制 token → `POST /api/notify {"token":"...","enabled":true}` → `POST /api/notify/test` 收到测试消息 → 等下一个整点+5min 确认调度运行（events 模式首轮静默播种，事件从第二轮起推）。**当前状态（第二十二/二十七轮）**：部署机与本机均已配置企业微信通道且 enabled=true，intervals=["1h","4h"]——**双机冗余推送**（两机指纹独立、配置需分别改、偶发重复消息属预期）；待用户在手机端确认收到带周期标签的事件消息即可关闭此项
6. 未排期迭代方向：AI 盘面解读（LLM 汇总各维度）、策略回测平台、可见区域成交量分布、按 interval 动态轮询周期、美股/加密相关标的（MSTR/COIN/NDX）、微信推送前端设置面板
7. 未排期（第十九轮可行 + 第二十轮调参被拒维持基线，**待用户凭收益表格决策**）：产品化（东财数据源接 `services/`、A 股视图/Tab、若接入则日志与仓位模块对 A 股品种的适配）。若未来放弃最差成员守卫换池化提升，须重新预登记另开调参轮（见 DEVLOG 第二十轮）
8. 第二十一轮已完成（盲测已烧、结论已记录）。可选后续：东财封禁窗过后 `--fetch-only` 补 4 个缺失驱动指数（SPX/N225/HSI/HSTECH，secid 届时单发核实）→ 隔夜因子若要翻案须**重新预登记另开一轮**（本轮敏感性已看，不可复用盲段）；pytdx（60m 最厚数据源）未验证，若未来做 60m 先导研究再探

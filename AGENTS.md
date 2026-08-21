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
  ├─ REST /api/* ──→ FastAPI 后端(:8000)
  │                    ├─ services/binance.py    币安数据(主源+镜像failover+冷却)
  │                    └─ services/analysis/     swings→smc→indicators→volume→decision
  └─ WebSocket ──→ wss://fstream.binance.com (失败降级 wss://stream.binance.com:9443)
```

布局：顶部 Header（搜索/周期/WS状态）｜左侧 klinecharts 主图+SMC 标注+EMA/RSI｜右侧 360px 栏：决策摘要卡 → 衍生品面板 → 成交量分布。

## 4. API 契约（前后端共同遵守）

- `GET /api/health` → `{"ok": true}`
- `GET /api/symbols?q=` → `[{"symbol","base"}]`（≤50 条）
- `GET /api/analysis?symbol&interval&limit` → `{symbol, interval, candles[], smc{swings, structureEvents, orderBlocks, fvgs, liquidityPools, premiumDiscount}, indicators{ema20/50/200, rsi14, atr14, adx14}(与candles等长含null), volumeProfile{poc,vah,val,bins}, summary{score(-100~100), bias, regime, keyLevels[], reasons[]}}`
  - interval ∈ {15m,1h,4h,1d}，limit 100~1000；评分 clamp ±100；bias: ≥15 bullish / ≤-15 bearish；reasons 中文按 |weight| 降序
- `GET /api/derivatives?symbol` → `{openInterest, openInterestValue, oiChangePct24h, oiHistory[], fundingRate, fundingHistory[], longShortRatio, longShortHistory[], takerBuySellRatio, source('binance'|'gateio'|null), options{atmIv, putCallRatio, contracts, expiry}}`（币安合约优先 → **Gate.io 回退**（futures tickers + contract_stats + contracts 规格 + options/tickers），任何字段可为 null）
- `GET /api/backtest?symbol&interval&limit&horizon` → `{samples, directionalSamples, ic, hitRate, scoreSeries[]}`（轻量评分 walk-forward：结构/EMA/RSI/溢价折价/CVD 背离按 regime 分化权重复算，IC=Spearman 相关）
- `GET /api/calendar` → `{events[{date,time,title,impact,kind}], note}`（本地维护 `backend/data/events.json`，网络封锁无法拉取宏观日历 API）
- 前端实时行情 WS：`{symbol小写}@kline_{interval}`，`data.k={t,o,h,l,c,v}`；新 K 线防抖 3s 重拉 analysis
- **analysis 响应增强字段**：`smc.orderBlocks[].quality / fvgs[].quality`（0-100）、`smc.sweepEvents[]`（扫流动性事件：side+outcome reclaimed/broken）、`indicators.cvd[]`（累计主动买卖差，来自 K 线 takerBuy 字段）、`volumeProfile.pocSeries[]/developingPoc`（滚动 POC）、`patterns{candles[],charts[]}`、`wyckoff{phase,events[]}`、`volatility{atrPct,bandwidthPct,squeeze,state}`、`cvdDivergence`、`mtf{list[{interval,score,bias}],alignment}`、`summary.tradePlan{direction,entry,stop,target1,target2,rr,note}`

## 5. 当前进度

### ✅ 后端（backend/）——已完成并验证通过
- 全部文件就绪：`main.py, config.py, services/{binance.py, analysis/{swings,smc,indicators,volume,decision}.py}, routers/{symbols,analysis,derivatives}.py, tests/{test_smc.py, verify_api.py}`
- venv 在 `backend/.venv`，启动：`.\.venv\Scripts\python.exe main.py`
- 验证结果：SMC 引擎单测通过（BOS/OB/FVG 识别正确）；/api/analysis BTC/ETH/SOL 均 200（BTC 1h: score=22 bullish trending，OB=10/FVG=10/pools=8）；边界校验 400 正常
- **failover 已按用户要求实现**（官方域名优先，失败才走镜像）：`services/binance.py` 主源 4s 短超时 → 失败标记主机冷却 300s（后续请求快速短路）→ 仅 klines/exchangeInfo 回退镜像；合约专属接口无镜像→路由层置 null。实测：首次请求 6s（含探测），冷却期内 1.8s

### ✅ 前端（frontend/）——已完成，构建与联调通过
- 全部文件就绪：`src/{main.tsx, App.tsx, App.css, types.ts, vite-env.d.ts, api/client.ts, utils/format.ts, ws/binanceWs.ts, components/{Header,SymbolSearch,ChartPanel,smcOverlays.ts,DecisionCard,DerivativesPanel,VolumeProfilePanel}.tsx}` + `tools/dns-override.cjs`
- `npm.cmd run build` 通过（tsc 零错误）；dev server :5173 全模块编译 200；`/api` 代理到后端验证可用
- **klinecharts 10.0.2 API 已对照 .d.ts 核对修正**，与 v9 差异点（后续维护必读）：
  - 数据只能通过 `chart.setDataLoader({getBars, subscribeBar, unsubscribeBar})` 喂入，`applyNewData/updateData` 已删除
  - `getBars({type: 'init'|'forward'|'backward', callback})`：init 首次拉取；forward/backward 为滚动分页（MVP 返回空+more=false，即无历史翻页）
  - 实时推送走 `subscribeBar({callback})`，图表在 init 完成后自动调用、symbol/period 变化时自动 unsubscribe——**WS 生命周期由图表管理**，App 只通过回调感知（防抖刷新 analysis）
  - `createIndicator(value, isStack)` 只有两个参数，pane 用 value.paneId 指定（不存在则自动建 pane，如 RSI_PANE）；内置 EMA/RSI/VOL 直接覆盖 calcParams 即可（EMA [20,50,200]、RSI [14]）
  - KLineData 字段是 `timestamp`（不是 time）；自定义 overlay 用 `registerOverlay` + `createPointFigures` 返回 Figure 数组（type: rect/line/text + attrs + styles）
  - 内置 `simpleTag` 用于流动性池/均衡位水平线（带 Y 轴价格标签）；自注册 `smcRect`（OB/FVG 矩形延伸至右缘）与 `smcText`（BOS/CHoCH 标注）
- 图表数据流：symbol/interval 变化 → setPeriod+setSymbol → loader.getBars 拉 analysis → onAnalysis 上报 App → overlays 按 groupId 重建；新 K 线 → App 防抖 3s 刷新 analysis（仅 overlays/面板，K 线本体由 WS 推送）
- **WS 断开时的 60s 轮询降级**（用户要求）：App 监听 wsStatus==='closed' → 每 60s refreshAnalysis() 并通过 `chartRef.pushBar(最后一根K线)` 原地更新图表（`_addData` update 语义：时间戳相等则更新、更大则追加，不重置视图）；WS 恢复自动切回推送。Header 状态点显示"轮询 60s"（琥珀色）
- **数据来源 tooltip**：`components/SourceHint.tsx`，面板标题旁问号 hover 显示来源说明+可点击网页链接（Coinglass 持仓量/资金费率/多空比、币安合约盘、TradingView）；derivatives 全 null 时空态里也内嵌链接

### 🔧 npm 网络问题的解法（重要）
本机企业 DNS 把 `registry.npmjs.org`/`registry.npmmirror.com` 劫持到 127.0.0.1（黑洞），导致 npm 静默失败；Zscaler 代理(127.0.0.1:9000)也封 registry，但**直连真实 IP 可通**（DNS 层劫持、IP 层未封）。
解决：`frontend/tools/dns-override.cjs`（Node `--require` 钩子，仅对当前进程把两个 registry 域名映射回真实 IP），用法：
```powershell
$env:NODE_OPTIONS='--require C:\dev\Coin\frontend\tools\dns-override.cjs'; npm.cmd install
```
`start-frontend.ps1` 已内置。若未来 npm 报 ECONNREFUSED，先确认该钩子仍生效（IP 可能变化：npmmirror 用 223.5.5.5 解析、npmjs 用 8.8.8.8 解析后更新 cjs 里的 MAP）。

## 6. 网络环境实测结论（重要）

本机企业网关（Zscaler PAC，代理 127.0.0.1:9000）实测：
| 源 | 可达 |
|---|---|
| fapi.binance.com / api.binance.com / api1 / fstream WS | ❌ 全部超时/RST |
| **data-api.binance.vision**（现货行情镜像，K线/exchangeInfo） | ✅ |
| **api.gateio.ws**（现货+**期权**，722 个 BTC 期权合约） | ✅ |
| data.binance.vision / bybit / okx / kucoin / coingecko | ❌ |

推论：
1. 本网络下 K线走镜像、OI/资金费率/多空比全部 null（合约接口无镜像）→ **衍生品数据恢复方案：二期接 Gate.io 期货+期权**（options OI/IV、合约 ticker），需加数据源适配层
2. 币安 WS 全被封 → 前端实时行情在当前网络下不可用；备选：轮询镜像 K线 REST（2~5s）或 Gate.io WS（未测试，待验证 `wss://api.gateio.ws/ws/v4/`）
3. 用户日常网络若可直连币安，代码无需改动（官方优先，自动全量）

## 7. 下一步（按序）

1. ~~修 npm install~~ ✅ 已解决（dns-override.cjs，见 §5）
2. ~~补写前端 5 个文件~~ ✅
3. ~~对照 klinecharts .d.ts 修正 ChartPanel API~~ ✅（v10 DataLoader 模型重构完成）
4. ~~`npm.cmd run build` + dev server 冒烟~~ ✅（tsc 零错误，模块全 200）
5. ~~前后端联调~~ ✅（/api 代理链路验证：health/analysis/derivatives 全通；首次 analysis 6s 含 failover 探测，冷却后 1.3s）
6. ~~启动脚本~~ ✅（start-backend.ps1 / start-frontend.ps1）
7. ~~用户浏览器人工验收~~ ✅（用户反馈"UI 不错"）
8. ~~WS 断开 60s 轮询降级 + 数据来源 tooltip~~ ✅（见 §5 前端章节）
9. ~~P0/P1/P2 分析策略增强全部完成~~ ✅（见 §8）
10. ~~决策引擎回测校准（7 轮循环，见 §9）~~ ✅（引擎 v3：CVD 共振组件、零权重负贡献组件、可执行保本管理计划）
11. ~~用户浏览器验收增强版 UI~~（部分：用户早期反馈"UI 不错"；增强版待刷新 http://localhost:5173 复核——多周期条、交易计划卡含保本移损行与回测统计、期权卡、CVD 副图、动态 POC 线、预警铃铛）
12. ~~提交首次 git commit~~ ✅（2026-08-21 `8ca7bd7`，62 文件：后端+前端+测试+文档+启动脚本；.venv/node_modules/dist/缓存已忽略）
13. 已知限制（后续迭代）：a) 滚动无历史分页（getBars forward/backward 返回空）；b) 轮询模式下新 K 线最长延迟 60s（可按 interval 动态调整轮询周期）；c) 图表形态识别是启发式（双顶/头肩/三角），置信度仅供参考；d) 事件日历为本地手动维护

## 8. 分析/策略增强 backlog（P0/P1/P2 全部完成 ✅）

### P0 —— 直接提升结论质量
1. ✅ **多周期共振（MTF）**：`/api/analysis` 内联 `mtf` 字段（15m→[1h,4h]、1h→[4h,1d]、4h→[1d]），并行拉取高周期 K 线（60s 缓存）跑同一套引擎取 bias/score；决策加共振权重 ±15；前端 MtfBar 显示周期芯片 + 共振/冲突标签。实测 BTC 1h：4h:bullish:67 + 1d:bullish:19 → aligned
2. ✅ **Regime 分化评分**：decision.py 按 ADX≥25 分支两套权重（趋势市：结构±35/EMA±10/OB±8/RSI 不减分；震荡市：结构±12/OB±12/RSI 反向±10/溢价折价±12），reasons 文案标注"趋势市权重/震荡市权重"
3. ✅ **OB/FVG 质量过滤**：OB 仅保留 displacement≥0.8×ATR 起源（突破段相对 ATR 强度），quality=displacement(60%)+放量(40%)+回踩守住 bonus(15)，按质量排序取前 10；FVG 过滤 <0.1×ATR 噪音 gap，quality 同理；决策权重按质量缩放（0.5~1.0 倍）
4. ✅ **CVD/主动买卖盘**：利用 K 线自带 takerBuy 字段（fapi 与镜像都有，索引 9）计算每根 delta=2×takerBuy-volume 累加成 CVD 曲线；检测近 30 根价格/CVD 背离（价涨 CVD 跌=虚假突破），决策 ±8/±10；前端新增 CVD 副图 pane

### P1 —— 补全逻辑链
5. ✅ **Wyckoff 阶段识别**：`wyckoff.py`——近 60 根区间检测（宽度≤3.5×ATR 为区间），价格位置+成交量萎缩判吸筹/派发；事件：Spring（刺破下沿收回）/UTAD（上冲回落）/SOS（放量突破）；阶段进决策权重 ±6/±8，事件进 reasons；图表上以青色文字标注
6. ✅ **K线/图表形态**：`patterns.py`——K 线形态（吞没/PinBar/内包/晨星/暮星，近 60 根）+ 图表形态（双顶/双底/头肩顶底/三种三角，基于摆动点+收盘确认+置信度）；进决策 ±3~±10；图表标注形态名
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
- 宏观联动（DXY/纳指相关性）、策略回测平台、交易日记
- 可见区域成交量分布、按 interval 动态轮询周期、滚动历史分页

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

### 走样本前向校准，非亏损率 ~91%（2026-08-21 第 8 轮，用户要求 90% 目标）
- 工具：`tests/plan_sweep.py`（日志 `tests/sweep.log`）。协议：时序 40%/30%/30% 三段（A 调参 → B 盲测 → A+B 重调 → C 盲测）；36 格粗粒度几何网格 + 4 门控；**EV 硬约束 ≥ +0.05R**（防"目标缩水刷胜率"）；超时单按市价结算（比旧口径更严格）
- **两阶段独立选出同一配置**（稳健平台而非刀锋拟合）：回踩 0.75×ATR / 止损 1.5×ATR / 保本触发 +0.25R / 目标 0.75R+1.5R / 96 根时间退出
- 结果：A 93.1% → **B 盲测 94.9%**（n=352）；A+B 重调 93.8% → **C 盲测 91.0%**（n=356，EV +0.148R）；C 段抽稀 91.2%/90.4%；分币种 92.3%/90.4%/90.1%（全 >90%）
- 参数面单调平滑（止损 1.5>1.0、保本 0.25>0.35>0.5 方向一致符合直觉），非噪声挑选；代价是单笔盈利缩小（EV 从 +0.25R 降到 +0.15R），用期望换确定性
- 生产引擎 `decision.py` 已切换到该几何；TradePlanCard 文案同步（含方法论 tooltip）

### 维护注意
- decision.py 权重注释已记录校准依据；改权重前必须重跑 `backtest_decision.py` 并检查 IS/OOS 两期一致性
- `_bt_cache.pkl` 会因 services/analysis/*.py 源码变化自动失效重算（约 8 分钟）

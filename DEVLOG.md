# CoinLens — 开发日志（DEVLOG.md）

> 按日期记录的开发/校准历史：每轮的背景、做法、实测数据、结论与被否决的方案。
> 规范、API 契约与当前状态见 `AGENTS.md`——会话开始先读那个；本文件按需查阅（需要了解某功能"为什么这样做"、回测依据、踩坑记录时再来读）。
> 约定：每次推进后在本文件**顶部**追加新条目（最新在前）。

## 2026-08-27（第十六轮）VPN 系统代理接入数据源链（fapi/Yahoo 复活）

- **背景**：用户连上 VPN 后要求重测。实测发现 VPN 是**系统代理模式**（注册表 ProxyEnable=1 + ProxyServer=127.0.0.1:13059，非 TUN 全局接管）——浏览器走代理，但 curl/python 直连不走（出口仍是电信 2408: 直连、fapi 仍解析到污染 IP 65.49.68.152/2001::）。**显式走代理（curl -x / httpx proxy=）后 fapi 全端点（含此前无镜像可用的 premiumIndex/futures/data）+ Yahoo query1/2 全部 200**（出口 IP 104.234.240.117）。
- **决策**：用户选择"两者都接"——币安链与宏观链都利用系统代理。设计原则：**代理是优先级链中的一环而非全局开关**，VPN 关闭时零配置自动回退既有降级链（镜像/Gate.io）。
- **实现**：
  - 新增 `services/sysproxy.py`：运行时读注册表探测系统代理（60s TTL 缓存；支持 host:port / http:// / 按协议分号格式，socks-only 配置如实返回 None——需 socksio 不支持）；传输失败（代理不可达/僵死连接）`mark_down` 300s 快速失败（与主机冷却一致）；共享代理连接池带第十五轮同款 keepalive_expiry+僵死换池重试；`ProxyUnavailable` 异常区分"没有代理/代理坏了"与"代理工作但目标返回状态码"（后者不标记代理坏——如 VPN 出口被币安区域封锁）
  - `binance.py`：优先级链插入"同主机经系统代理"环节——`_get`（K线/exchangeInfo/ticker24h/premiumIndex/futures/data 全走它）与 `get_depth`（订单簿）均为 直连 fapi → fapi 经代理 → 镜像/其他源；代理环节用独立冷却键 `fapi|sysproxy`（直连失败不阻塞代理尝试、代理失败不阻塞直连重试）；`host_status()` 增列代理环节
  - `macro.py`：`_yahoo_chart` 路由计划=直连双主机→代理双主机；直连 403 且无代理=立即 `_YahooBlocked`（保持第十五轮快速失败）；**全路由封锁（直连 403+代理不可用/403）→ 15 分钟 `_blocked_until` 快速失败窗**，防止 7 序列×2 备用符号每序列重复撞墙把 /api/macro 拖到分钟级（踩坑预防：闭包里给模块级 `_blocked_until` 赋值必须 `global` 声明，否则静默写进局部变量）
  - `routers/sources.py`：链序描述更新（各链插入"·系统代理"环节）+ `systemProxy{url,down,retryInS}` 状态字段；`main.py` shutdown 钩子增 `sysproxy.close_client()`
- **验证**（重启后端 PID 12524，全端点 200）：
  | 端点 | 结果 |
  |---|---|
  | derivatives | **source=binance（经代理）**——真实币安 OI $8.34B（Gate 口径仅 $4.57B）、funding 3.6e-05、taker 1.24、historyStats 1002 天分位、Gate options（658 张/maxPain 77000）并存 |
  | orderbook | **source=binance_perp（经代理）**，spreadBps 0.01 |
  | macro | **解冻**——7 序列经代理续更至 08-26/27（冻结了 3 天），首刷 33.2s（每序列直连 403×2+代理 200），二次调用 0.1s（缓存） |
  | analysis / klines / backtest / liquidations / onchain / symbols | 全部 200 正常 |
- **诚实口径**：①`topTraderRatio` 在 source=binance 时为 null——Gate 独有字段（top_lsr_size），路由原设计即"仅 Gate 回退时填充"，08-24 fapi 可达的正常年代同样如此，非本轮回归；②经代理的流量走 VPN 出口（104.234.240.117），延迟 ~1.2s/请求且依赖 VPN 稳定性——代理失败 300s 冷却自动回退镜像/Gate；③Yahoo 代理路由下每日首次刷新 ~33s（7 序列×限速 1.6s 间隔），之后当日缓存。
- **无策略参数改动**；优化不重开。

## 2026-08-27（第十五轮）网络复查：fapi DNS 污染 + Yahoo 403 + 长跑进程连接池僵死（加固+重启）

- **背景**：用户要求确认当前网络下数据接口是否仍正常（对照 AGENTS §6/§5 的既有实测记录）。
- **网络实测（与 08-25 记录的差异）**：
  - ❌ **fapi.binance.com 被 DNS 污染**——Resolve-DnsName/getaddrinfo 返回假 IP 轮换（104.244.43.231[Twitter 段]/69.63.186.30[Facebook 段]/2001::…），TCP 全部超时（08-24 时还可达）。与 npm registry 被企业 DNS 劫持同一手法：DNS 层污染、IP 层未封
  - ✅ data-api.binance.vision、api.gateio.ws（tickers/funding_rate/contract_stats/options/contracts/order_book 全端点）、mempool.space、api.blockchain.info 全部 200
  - ⚠️ **Yahoo query1/2 一律 403**（curl 与后端同款 httpx+浏览器 UA 双通道、加 cookie jar 均拒）——macro.db 七个序列全部停在 2026-08-24，即 08-25 起 Yahoo 已不可用（此前靠旧进程 30min 内存缓存 + <2 天不重拉的 SQLite 判定掩盖）
- **顺带发现并修复——长跑后端进程连接池僵死**：8000 端口后端（08-25 23:20 启动，跑 1.5 天）进程内对镜像/Gate 的请求**全部失败**（analysis 502 "Binance hosts unreachable (cooldown)"、derivatives/orderbook 快照字段全空），而同机新进程 httpx 直测全部 200——是**进程状态问题而非网络问题**（liquidations/onchain/macro 正常是因为它们各自独立建 client/读本地库）。用 8001 临时实例验证降级链本身工作正常（orderbook→gateio_perp、derivatives→gateio、analysis 200）。
- **加固（binance.py/gateio.py）**：①共享 client 改 `httpx.Limits(keepalive_expiry=60)` 主动剔除闲置连接——**坑：httpx 0.28.1 已删除 transport/AsyncClient 的 keepalive_expiry 参数，只能经 Limits 传**；②`_fetch/_get` 传输错误时（ConnectTimeout 除外——从未连上=真不可达，不是僵死连接）用一次性新 client 重试一次，成功则 `_swap_client` 换池。第十二轮"勿改回每请求新建 client"的约定仍然成立——错误路径的一次性重建是防御逻辑。
- **macro.py 403 快速失败**：403 是封锁不是限流，原 429 重试协议在新网络下每序列空转 ~15-35s（冷启动宏观接口 >2min 客户端超时）；新增 `_YahooBlocked` 直接抛出不重试，宏观接口 12.2s 返回冻结在 08-24 的缓存序列（诚实降级，网络恢复自动续更）。
- **重启与验证**：杀旧进程（venv launcher 父子两个 PID 都杀）→ `Start-Process python main.py` 后台启动（日志 `backend/data/uvicorn-8000.log`）→ 全端点验证 200：health / analysis（score 52 bullish plan long，与重启前新进程结果一致）/ derivatives（gateio：funding/OI $4.57B/topTrader/atmIv 全有）/ orderbook（gateio_perp）/ liquidations / onchain / macro（7 序列）；`/api/sources` 显示 fapi 冷却中（符合预期：每 300s 探测、4s 快速失败）、vision 正常。
- **遗留（诚实口径）**：币安合约独有端点（premiumIndex、futures/data 系列）在 fapi 污染期间无镜像可用，衍生品数据由 Gate.io contract_stats 等价覆盖（实测字段无缺）；宏观冻结在 08-24 直至 Yahoo 恢复或换源（stooq 已下线、无其他免费可达源）；DNS 污染随运营商策略随时可能变化，一切以运行时探测为准。无策略参数改动。

## 2026-08-26（第十四轮）用户 Pine 脚本 vs 生产策略对比回测（用户问答）

- **背景**：用户提供三个 TradingView Pine v6 脚本（`C:\Users\Administrator\Downloads\pine\{btc,eth,sol}1h.txt`，KC 通道突破 + EMA 趋势过滤 + ADX 区间 + RSI + 量能确认入场，三段式 ATR 止损），要求并发简单回测并与当前生产策略比较优劣。
- **工具**：新增 `backend/tests/backtest_pine.py`（每 symbol 一个 multiprocessing worker，§7.8 合规；K 线走 kline_cache，CoinLens 侧直接复用 `_5y_cache_*` 1h 五年决策记录缓存——引擎哈希校验通过，零重算，秒级出结果）。
- **Pine 复刻要点（逐行核对脚本）**：信号收盘评估、下一根开盘成交；入场当根无止损（strategy.exit 收盘下单下一根生效）；止损每根用**当前 ATR** 重算（保本前随 ATR 漂移，与脚本一致）；BTC/SOL 空头入场被注释=纯多头；ETH 多空双向（空头 KC 倍数 0→下轨退化为 EMA25）；SOL 多头 ADX 为 dmi(14,14)；BTC/ETH 多头趋势过滤用 EMA(680) 斜率、SOL 注释掉；冷却计数与仓位状态无关每信号推进；pyramiding=0、反向信号下一根开盘反手；佣金 0.04%/边、percent_of_equity 复利（BTC/SOL 90%、ETH 96%）；空头减半块全被注释→忽略。指标用 pandas ewm（EMA 与 Pine 同递归种子；RMA/RSI/ADX 的 SMA 种子差异 3000 根 warmup 后归零）。同根多空双信号边界 case 实测 0 次。
- **数据窗口**：两策略完全同一 df=最新 43800 根 1h（2021-08-27..2026-08-26），同一手续费模型（0.04%/边）。CoinLens 侧 ~24 条早于 df 起点的记录被跳过（可忽略）；仓位口径=BACKTEST.md 的 1% 权益风险/笔，费用折算进 R。
- **结果**（5 年，费后）：

  | | 笔数 | 胜率 | 总收益 | 最大回撤 | MAR | EV/笔 | 扣费总R |
  |---|---|---|---|---|---|---|---|
  | Pine BTC（90%名义，止损≈4.9%价距） | 150 | 63.3% | +109.6% | 20.7% | 0.84 | +0.210R | +29.2R |
  | Pine ETH（96%名义，多空） | 265 | 63.0% | +603.6% | 38.1% | 1.37 | +0.452R | +115.7R |
  | Pine SOL（90%名义，止损≈13.5%价距） | 223 | 83.4% | +858.7% | 24.4% | 2.56 | +0.178R | +39.6R |
  | CoinLens 1h BTC（1%风险） | 3527 | 96.6% | +4980% | 4.4% | 27.7 | +0.112R | +394.5R（毛+619.7R） |
  | CoinLens 1h ETH | 3575 | 96.7% | +7069% | 3.2% | 43.3 | +0.120R | +428.9R（毛+597.7R） |
  | CoinLens 1h SOL | 3757 | 97.7% | +18608% | 2.7% | 70.7 | +0.140R | +524.7R（毛+647.0R） |

  Buy&Hold：BTC +61.5% / ETH −24.6% / SOL +12.6%。CoinLens 毛数字与 BACKTEST.md 第 4.3 节四年合计交叉印证一致（三币毛合计 +1864R）。分年：CoinLens 三币×每年全正；Pine BTC 2022/2026 亏损、SOL 2023 +330%（单年爆发）。
- **4h 补充行（用户追问"4h 是否比 1h 更稳"，同表同费率同 1% 风险口径）**：CoinLens 4h（几何 0.75/1.0/0.75R/跟踪0.35/48根、fill=18、阈值10 放宽，loose 记录）——BTC 871 笔 +2198%（DD 6.1%、MAR 15.5、净EV +0.364R/毛+368.8R）；ETH 867 笔 +3353%（DD 4.5%、MAR 24.6、净EV +0.412R/毛+395.5R）；SOL 877 笔 +3491%（DD 5.4%、MAR 21.1、净EV +0.412R/毛+388.6R）；三币×每年全正；毛 EV 与 BACKTEST.md 4.3 节四币合计 +0.426R 交叉印证一致。**1h vs 4h 结论**：4h 单笔 EV ~3.5 倍、笔数少 4 倍（人工遵循更现实），但权益回撤更大（4.5~6.1% vs 2.7~4.4%）、非亏损率更低（77~81% vs 97~98%）、MAR 更低（15~25 vs 28~71）——按权益曲线平滑度 **1h 更"稳"，按单笔效率与执行可行性 4h 更优**（与第九轮实践周期建议一致：人工实盘主用 4h）。
- **结论**：**当前生产策略显著更优**——风险调整后收益高一个数量级（MAR 28~71 vs 0.8~2.6）、回撤低一个数量级（3~4% vs 21~38%）、逐年全正、费后 EV×频率（+0.12R×~3600 笔 vs +0.28R×~210 笔）复利差距巨大。**诚实口径**：Pine 单笔 EV 更高（尤其 ETH +0.45R）且 5 年全正、跑赢 Buy&Hold——作为手调策略本身不差；差距主要来自执行层结构（回踩限价+减半保本+高频复利 vs 宽止损趋势单）。仓位口径不同（Pine 每笔风险≈4~12% 权益 vs CoinLens 1%），收益绝对值不可直接比，MAR/回撤才是可比口径。BTC/ETH/SOL 是 CoinLens 调参标的（Pine 也是用户手调，双方均"主场"），结论以 LTC/Top10 样本外轮交叉印证为准。
- **无生产代码改动**；新增测试脚本一处。优化不重开（§7.3）。

## 2026-08-25（第十三轮）外网访问：Cloudflare Tunnel 打通（替代端口转发）

- **背景**：用户要求手机/电脑不在同一局域网时能访问 UI，先试路由器端口转发+DDNS。
- **端口转发探测（不可行，证据链）**：网关 TL-WDR5620(192.168.0.1) UPnP 在线，但 `GetExternalIPAddress` 返回 **192.168.1.6（私网）**→ 上游还有一层电信"超级家庭网关"光猫（192.168.1.1，UPnP 关闭）；tracert 第 3 跳 **10.220.224.1（运营商 CGNAT 私网段）**——宽带线路无公网 IPv4，转发无从转起。本机 ipify 查到的 38.54.32.253 是 Zscaler 曼谷代理出口，非线路真实 IP（查公网 IP 必须走 UPnP/tracert 这类带外手段）。
- **备选路径**：公网 IPv6 直连已就绪（2408:8240:c15:b8e4::1000、Vite 双栈、防火墙放行、本机自测 200），但依赖手机侧有 IPv6 且光猫 IPv6 防火墙未拦入站，未最终验证。
- **采纳：Cloudflare Tunnel**：`tools\cloudflared.exe`（GitHub Releases 直链经 Zscaler 代理下载，第一次 EOF 中断、重试成功）+ quick tunnel 免账号模式，`--protocol http2` 强制 TCP（Zscaler 封 UDP QUIC）。**踩坑**：首次访问 403——Vite 6 默认校验 Host 头，`vite.config.ts` 需配 `allowedHosts: ['.trycloudflare.com']`（后缀匹配，quick tunnel 每次重启随机换域名）；修复后根页面与 `/api/health` 经隧道全部 200（Cloudflare 边缘 → tunnel → Vite → /api 代理 → FastAPI 全链路）。
- **诚实口径**：quick tunnel 免费无 SLA、URL 重启即变、**公开可访问（无登录体系，拿到链接即可用，勿外传）**；长期使用应升级为 named tunnel（需 Cloudflare 账号+域名，可配 Access 认证）。隧道进程当前以后台任务运行，仅本会话生命周期。
- **文档**：AGENTS.md 运行环境（隧道命令+踩坑+备选 IPv6）；.gitignore 加 `tools/cloudflared.exe`（52MB 二进制不进库）。
- **验收**：用户手机实测外网访问 `https://extent-crossing-objectives-stevens.trycloudflare.com` 成功（2026-08-25 23:42 确认可用）。

## 2026-08-25（第十二轮）加载性能优化（连接池+并行化，整页 ~15s→~2.4s）

- **背景**：用户反馈 UI 加载慢（K 线/分析等），要求优化（多线程/进程）。先实测定位再动手——**CPU 不是瓶颈**（`full_analysis` 500 根仅 13ms），瓶颈是**网络串行等待**：①binance/gateio 每次请求新建 `httpx.AsyncClient`（企业网 TLS 握手 1-2s/次）；②derivatives 路由 5 个币安调用顺序 await（最坏叠加 10s+）；③`ensure_backfill`（Gate.io 回填，首次 1d×1000+1h×720）阻塞响应；④前端 `refreshData` 先 await analysis 完才触发其余面板、订单簿/清算/链上/宏观各自内部串行。
- **后端改动**：
  - `binance.py`/`gateio.py`：**共享懒加载 AsyncClient 连接池**（keep-alive 复用 TCP+TLS；按请求覆盖超时；`close_client()` 由 FastAPI shutdown 钩子清理）——所有走 `_fetch/_get` 的路径（K 线/订单簿/衍生品/清算/扫描）自动受益
  - `routers/derivatives.py`：5 个币安端点 `asyncio.gather` 并行；Gate.io 回退与 options 快照并行；`ensure_backfill` 移到 `asyncio.create_task` 后台任务（强引用防 GC；快照入库/分位数读取仍内联）——响应不再被回填阻塞
  - `gateio.futures_snapshot` 内部 tickers/stats/spec 三连请求并行
  - `routers/analysis.py` `_derivatives_context`：OI 历史/费率两个调用并行
- **前端改动**（`App.tsx`）：`refreshData` 中各面板加载（衍生品/回测/订单簿/清算/链上/宏观/扫描观察）与 analysis **并行发出**（原来 analysis 完才轮到它们）；`loadMarketPanels`/`loadGlobalPanels` 内部改 `Promise.allSettled` 并行
- **实测**（本机 Zscaler 网络，冷启动/热连接）：
  | 端点 | 优化前 | 优化后（冷） | 优化后（热） |
  |---|---|---|---|
  | analysis 4h 500 根 | 7.9s | 2.4~3.9s | **0.46s** |
  | derivatives | 14.8s | 2.0~3.0s | **1.2s** |
  | liquidations | 4.0s | 0.8~1.9s | ~1.0s |
  | orderbook | 1.2s | 0.45s | 0.01s（内存缓存） |
  | 整页（前端并行刷新） | ~15s（被 derivatives 卡死） | ~2.4s | ~0.5s |
- **正确性验证**：SOL 4h 实时决策与优化前逐字段一致（score 37/bullish/CVD 共振 bearish×2/plan short 100.27/新几何 stopAtr=1.0）；5 个端点全部 200；tsc 零错误。
- **诚实口径**：首次冷启动仍要 2-4s（真实网络往返）；derivatives 的 historyStats 在**全新币种首次调用**时可能滞后一拍（回填在后台跑，下次刷新补上）——既有行为不变，只是不再阻塞响应。
- **多进程方案说明**：用户建议的多线程/进程不适用于此问题（CPU 仅 13ms，等待全在网络 IO）——异步并发+连接池才是对症方案；回测类脚本的多进程规范已于第九轮固化（AGENTS §7.8）。

## 2026-08-25（第十一轮）"评分方向 vs 计划方向冲突"case 统计（用户问答）

- **背景**：用户看到 SOL 4h 评分 +37 看多但计划做空（CVD 多周期共振覆盖评分方向），问应遵循还是放弃、历史回测里多少这种 case、放弃会损失多少利润。
- **工具**：临时脚本复用 5 年缓存记录（`_5y_cache_*`，秒级）+ CONF5 新几何容量约束串行重放；冲突=计划方向与评分符号相反（全部来自 CVD 共振覆盖分支，与生产方向逻辑一致）；另跑"放弃冲突挂单"变体（容量让给下一信号）对比总利润。
- **结果**（未计手续费）：
  - **4h 四币 5 年合计**：成交 3517 笔中冲突 134 笔（**3.8%**）；冲突单 +41.1R（EV+0.307R、胜率 73.9%）vs 一致单 +1480.4R（EV+0.438R、胜率 79.4%）；**全部放弃冲突单 → 总利润 1521.6→1493.5R（−28.1R / −1.8%）**。分币冲突 EV：BTC +0.064R（34 笔弱正）、ETH +0.488、BNB +0.279、SOL +0.438（42 笔，与一致单 +0.443 打平）。
  - **1h 四币 5 年合计**：冲突 1629/14223 笔（11.4%），+229.0R（EV+0.140R vs 一致 +0.175R），放弃 → −182.1R（−7.5%）。
- **结论**：冲突单**系统性盈利**（EV 略低于一致单但为正，SOL 上与一致单打平，每年分年全正居多）——**遵循计划、不放弃**；放弃=引入未经验证的自由裁量过滤，5 年少赚 ~28R（4h）/~182R（1h），且破坏"回测验证的就是这套规则"的口径。
- **顺带发现并修复**：8000 端口被当日 00:03 启动的旧会话后端进程（第 11 轮旧几何）占用，白天 UI 一直由旧几何服务（计划卡 note 显示 1.2×ATR 止损/0.5R 跟踪暴露了这一点）；已杀掉并以当前代码重启（新几何 stopAtr=1.0/beR=0.75/trailR=0.35 已验证生效）。**教训：启动后端前先查端口占用**（已写入 AGENTS.md 运行环境）。

## 2026-08-25（第十轮）图表副图精简 + 交易计划置顶

- **背景**：用户要求①去掉 K 线图下方的 RSI/CVD 副图；②确认"bias 看多但计划做空时听谁的"并把交易计划卡放到决策 Tab 最上面（最重要的放最显眼）。
- **方向口径确认**（无代码改动，回答用户问题）：`decision.py _build_trade_plan` 中计划方向 = **CVD 多周期共振方向优先**，否则 |评分|≥阈值时取评分符号。因此"决策摘要 bias=看多、交易计划=做空"真实存在（如 1h 评分 +10 未达 25 阈值但 CVD 多周期共振看空）——**此时应遵循交易计划**：回测验证的正是"共振方向或达阈值的评分符号"这条入场规则（+2xxxR 全部由该层产生），bias 只是描述性汇总；计划卡 note 已标注方向来源（"CVD 多周期共振方向"/"结构评分方向"）。
- **改动**：`ChartPanel.tsx` 移除 RSI/CVD 副图（pane 常量、createIndicator/setPaneOptions、CVD 自定义指标注册与 extendData 刷新逻辑全删；EMA/成交量保留；RSI/CVD 数据仍在决策评分与滚动回测中使用，不受影响）；`App.tsx` 决策 Tab 内 TradePlanCard 移到 DecisionCard 之上；AGENTS.md §3 布局描述同步。
- **验证**：`npm.cmd run build` tsc 零错误；Vite dev HMR 生效后浏览器直接可见。

## 2026-08-25（第九轮）BNB 快捷按钮 + 回测并发规范固化 + 实践周期建议

- **背景**：用户要求 ①把 BNB 加入 Header 快捷币种；②后续任何回测脚本必须并发执行（现在太慢），写入 AGENTS.md；③在 1h 与 4h 之间选实践周期。
- **改动**：`types.ts` QUICK_SYMBOLS 增 BNB（第 4 个快捷按钮，SymbolSearch 自动渲染，`flex-wrap` 断点样式无需改）；AGENTS.md §7 新增规则 8「回测脚本必须并发执行」（multiprocessing 每 symbol×timeframe 一 worker + asyncio 网络段，串行长跑 >10 分钟不允许，既有串行脚本下次触碰时并行化），§3 Header 描述同步快捷币种列表。
- **实践周期建议**（无代码改动，基于本机复算的 5 年数字）：**主用 4h**——EV +0.43R（约 1h 的 2.5 倍）、每币每年约 175 笔，人工可维持高遵循率，手续费拖累占 EV 比例低（~0.03-0.06R/笔）；1h 总利润更高（~120R/币/年 vs ~76R）但前提是抓住每天 ~2 个信号且全程不偏离计划，人工执行难保证，且 97% 胜率依赖大量小额保本单；1d 每币每年仅 ~27 笔、学习反馈太慢。可在日记里先以 4h 实盘积累遵循率数据，1h 用观察/纸面跟踪。
- **验证**：`npm.cmd run build` tsc 零错误。

## 2026-08-25（第八轮）五年扩展回测 + 第 13 轮几何重校准（采纳）+ BACKTEST.md

- **背景**：用户要求①把回测结果输出到新文档；②BTC/ETH/BNB/SOL 采样扩到 5 年重跑；③"继续优化策略（如果仍有空间）"。
- **新文档**：根目录 `BACKTEST.md`——回测口径说明 + LTC 轮 + Top10 轮 + 5 年轮全量数字与结论（面向用户，与 DEVLOG 数字一一对应）。
- **工具**：`tests/backtest_5y.py`——窗口 1h 43800 / 4h 10950 / 1d 1825 / 1w 全历史；决策窗口/MTF/阈值/成交窗口与生产一致（CONF 复用 backtest_ltc 并扩窗口）；容量约束串行 + 同根保守顺序；记录缓存 `_5y_cache_{sym}_{tf}.pkl`（几何无关，重评估秒级复用）。**踩坑**：①后台进程 ~15 分钟被环境静默强杀（探针验证）→ 改前台分周期跑；②中文输出打到 cp1252 管道 UnicodeEncodeError 崩溃（`cmd /c` 重定向不吃 `$env:PYTHONIOENCODING`）→ 脚本头部 `sys.stdout.reconfigure(encoding='utf-8')` 自保护。全部周期跑完约 80 分钟（记录计算主导）。
- **5 年基线（旧=第 11 轮几何）**：四币合计 1h 10781 笔 98.0% EV+0.108R **+1166.2R** DD4.0R ｜ 4h 3522 笔 86.5% +0.302R **+1062.1R** DD5.7R ｜ 1d 565 笔 85.8% +0.258R **+145.8R** ｜ 1w 105 笔 92.4% +0.500R **+52.5R**；分币种四周期总利润 BTC+592.6/ETH+625.7/BNB+598.3/SOL+610.1R 几乎无差；1h/4h/1d 四币每个自然年全正（含 2022 深熊），1w 仅 ETH 2022 -0.0R 持平。
- **优化检查（§7.3 新数据条款：2021-08~2023-08 调参前时代为 1h/4h 新维度）**——协议在跑任何结果前预登记：1w 排除；A 40% 调参/B+C 盲测；单遍坐标下降（每轴 2 个预定义值，不重启网格、不碰阈值/成交窗口）；验收=盲测>+5% 且逐币>0 且最差币守卫 95%；C 段永不参与选择。
  - Phase 1（A 调参→B+C 一次盲测）：1h +1388.5 vs +718.1R（+93.4%）、4h +874.4 vs +615.6R（+42.0%）、1d +128.8 vs +85.5R（+50.7%）——三周期全过，逐币全改善。
  - Phase 2（A+B 重调→C 段一次盲测，第 11 轮出厂标准）：1h +714.0 vs +357.4R（**+99.8%**，EV+0.169R，胜率 97.1%，最差币 +156.1 vs +86.4）、4h +470.6 vs +315.3R（**+49.2%**，EV+0.446R，胜率 79.4%）、1d +53.6 vs +38.5R（**+38.9%**，EV+0.311R）——全过；1h/4h 与 Phase 1 收敛到相同几何（稳健性信号）。
  - 可信度：旧几何在第 11 轮调参数据（与盲测段重叠）有主场优势仍落败；四币盲测逐个改善；Phase1/2 独立收敛。代价：4h 胜率 86.5→79.4%（利润优先），DD 近似；1h DD 4.0→4.8R。
- **采纳**：`decision.py PLAN_GEOMETRY` 更新为 1h (0.5, 2.0, 0.15, 0.5, 96, None, 24)、4h (0.75, 1.0, 0.75, None, 48, 0.35, 18)、1d (1.0, 1.2, 0.5, None, 12, 0.35, 9)、1w 不变；阈值/成交窗口/评分权重零改动（仅几何）。docstring 记录第 13 轮校准依据。`backtest_ltc.CONF`（测试工具的生产口径唯一来源）同步。
- **验证**：SMC 单测通过；引擎冒烟（ETH 1h/BTC 4h/BNB 1d）新几何字段正确流入计划卡（journal_store/portfolio/position 路由均动态 import PLAN_GEOMETRY，自动生效）；评分路径结构性不受几何影响（build_summary 先于 _build_trade_plan）。前端计划卡字段动态渲染，无需改动。
- **文档同步**：BACKTEST.md（第 4 节全记录）、STRATEGY.md（计划规则表/成绩表/风险误区全部换新口径数字）、AGENTS.md（§4 契约几何、§5 状态、§7.3 终止令更新为"第 13 轮重开一次并再冻结"）。
- **诚实口径**：LTC/Top10 轮数字对应旧几何（保留作历史记录）；新几何全 5 年参考值 1h +2275.6R/4h +1483.2R/1d +192.9R（含调参段，盲测段见上）；未计手续费。**优化再次冻结**——无新数据维度不重启。

## 2026-08-25（第七轮）成交量前 10 币种回测排名

- **背景**：用户要求"回测成交量前 10 的币种，并按照当前策略的收益率排序"。
- **做法**：新工具 `tests/backtest_top10.py`——`backtest_ltc.CONF` 为生产口径唯一来源（PLAN_GEOMETRY/PLAN_THRESHOLD/窗口/MTF/成交窗口/容量约束串行/保守同根顺序，全部复用）；标的取当前 24h 成交额前 10（`scanner._top_usdt_pairs` 同款过滤）：BTC/ETH/SOL/XRP/ZEC/BNB/PYTH/DOGE/SUI/TUTU。BTC/ETH/SOL 为调参标的，其余 7 个纯样本外；PYTH/SUI/TUTU 历史不足跳过 1w（TUTU 1h 仅 ~17 个月）。记录缓存 `tests/_t10_cache_{sym}_{tf}.pkl`（已入 .gitignore）。运行 ~55 分钟。
- **结果**（各周期窗口与 LTC 轮一致：1h 2 年/4h 3 年/1d 4 年/1w 起始以来；未计手续费）：

| 排名 | 币种 | 总利润 | 笔数 | 每笔EV | 胜率 | 最大回撤 | PF | 性质 |
|---|---|---|---|---|---|---|---|---|
| 1 | BTC | +321.9R | 1798 | +0.179R | 93.4% | 4.0R | 3.70 | 调参 |
| 2 | ETH | +314.1R | 1681 | +0.187R | 93.8% | 2.6R | 4.02 | 调参 |
| 3 | BNB | +305.3R | 1647 | +0.185R | 93.3% | 4.2R | 3.75 | 样本外 |
| 4 | SOL | +303.1R | 1698 | +0.178R | 94.3% | 3.2R | 4.19 | 调参 |
| 5 | SUI | +282.9R | 1607 | +0.176R | 93.8% | 3.9R | 3.83 | 样本外 |
| 6 | DOGE | +282.1R | 1681 | +0.168R | 93.5% | 4.8R | 3.56 | 样本外 |
| 7 | ZEC | +278.7R | 1622 | +0.172R | 93.5% | 3.0R | 3.65 | 样本外 |
| 8 | PYTH | +274.9R | 1596 | +0.172R | 95.5% | 2.9R | 4.82 | 样本外(无1w) |
| 9 | XRP | +263.4R | 1732 | +0.152R | 92.4% | 5.1R | 3.01 | 样本外 |
| 10 | TUTU | +138.8R | 921 | +0.151R | 94.1% | 6.0R | 3.57 | 样本外(历史短) |

- **结论**：①**10/10 币种全部盈利、全部周期 EV 为正**（10 币 × 37 个币种-周期组合无一亏损）；②优势高度结构性：1h EV 全部落在 +0.086~+0.106R、4h 全部 +0.250~+0.381R——跨币种收敛而非个别币拟合，执行层优势的强证据；③调参三币占据 1/2/4 名但**纯样本外 BNB（+305.3R）落在调参标的区间内**，最好与最差完整历史币种差距仅 ~18%（321.9 vs 263.4）——无明显过拟合币种偏斜；④若按每笔 EV（收益率）排序：ETH(+0.187) > BNB(+0.185) > BTC(+0.179) > SOL=SUI(+0.178/+0.176)，与总利润排序基本一致；⑤LTC 轮（+292.6R 总/EV+0.20R）落在本排名第 4~5 名区间，交叉印证；⑥TUTU 4h EV 最高（+0.381R）但 1h 最弱（+0.086R）且仅 17 个月历史/1d 仅 8 笔——小样本不解读；⑦三币 4h 合计 +488.5R（全 3 年），与第 11 轮盲测 B+C 口径（+285.1R，约 60% 时段）量级吻合，交叉验证 harness 一致。
- **注意**：本轮为用户指定标的验证性回测，**未改任何生产参数**（策略优化终止令仍然有效）；总利润受历史长度影响（TUTU/PYTH 偏短），EV 列为归一化收益率。

## 2026-08-25（第六轮）LTC 纯样本外回测（用户指定标的验证）

- **背景**：用户要求"利用当前的策略，对 LTC 做个回测，看看结果如何"。LTC 从未参与任何调参（11b 轮扩展样本验证在 LTC 数据到达前被用户否决），因此这是生产配置（第 11b 轮冻结）的**纯样本外**测试。
- **做法**：新工具 `tests/backtest_ltc.py`——零改动复用第 11/12 轮已验证 harness：`profit_sweep2.decide_at/tf_summary_closed/build_plan`（决策采样+已收盘高周期 MTF）+ `profit2_cap.sim_outcome_full`（容量约束串行执行：同根保守顺序 stop>target>trigger、跟踪棘轮仅用先前 K 线）+ `profit2_r5.with_loose_plans`（4h/1d/1w 阈值放宽至 10）。生产口径逐周期落实：1h (0.75/2.5/0.1R/0.75R/96/无跟踪, fill 24, th 25)+MTF(4h+1d)、4h (0.75/1.2/0.5R/跟踪0.5R/48, fill 18, th 10)+MTF(1d)、1d (0.75/1.5/0.5R/跟踪0.5R/24, fill 9)、1w (0.75/1.5/0.5R/跟踪0.75R/24, fill 8)；决策窗口 1h/4h=500、1d=300、1w=170 根；与校准口径一致**不含资金费率/OI 组件**；未计手续费/滑点。记录缓存 `tests/_ltc_cache_*.pkl`（按引擎源码 hash 自动失效，已入 .gitignore）。
- **数据**：kline_cache 全命中——1h 2 年 17520 根、4h 3 年 6570 根、1d 4 年 1460 根、1w 455 根（2017-12 起）；决策点 4223/1498/525/253。
- **结果**（容量约束串行，挂单→成交→胜率/EV/总利润/最大回撤/PF）：

| 周期 | 成交 | 胜率=非亏损 | EV | 总利润 | 最大回撤 | PF | 分年 |
|---|---|---|---|---|---|---|---|
| 1h | 1110 | 98.7% | +0.105R | **+116.6R** | 1.0R | 9.33 | 24:+18.5 / 25:+59.2 / 26:+39.0 |
| 4h | 499 | 87.0% | +0.301R | **+150.4R** | 3.9R | 3.31 | 23:+8.2 / 24:+53.1 / 25:+49.9 / 26:+39.1 |
| 1d | 96 | 86.5% | +0.255R | **+24.5R** | 2.6R | 2.94 | 各年均为正（+3.7~+9.4） |
| 1w | 19 | 73.7% | +0.057R | +1.1R | 2.8R | 1.22 | 小样本，年度有正有负 |

- **结论**：①LTC 上策略完整成立——四个周期 EV 全正、1h/4h/1d 每个自然年利润全正，4h 量级与调参三币盲测基线（+285R/三币）同量级偏优；②**方向准确率 46.8%~48.3%（全部 <50%）而总利润为正**——再次实证核心承诺：利润来自执行层（回踩入场+减半保本+跟踪），不来自方向预测；③1w 仍小样本（19 笔）勉强为正，维持"样本不足"的既有声明；④未计手续费（maker ~0.02%，净 EV 再折）。
- **注意**：本轮为用户指定标的验证性回测，**未改任何生产参数**（策略优化终止令仍然有效）。

## 2026-08-25（第五轮）用户侧策略报告 STRATEGY.md

- **背景**：用户要求"把策略做个介绍，以及收益风险等，做个报告单独输出到一个文档，要求说人话，普通人容易理解"。
- **产出**：根目录 `STRATEGY.md`（纯文档，无代码改动）。结构：一句话总结（不预测、管执行）→ 系统是什么（四模块大白话表）→ 核心思想（方向≈抛硬币+执行层三件事+算账）→ 计划规则表（ATR/R 术语通俗定义+分周期几何+止损挂交易所提醒）→ 回测成绩表（1h LOSO 97-98%/4h +288R·87%·EV+0.315R·DD 5R/1d +47R/1w 46笔小样本，含 R→USDT 换算示例、"禁止年化外推"提醒）→ 九条风险局限 → 使用工作流 → 常见误区表。
- **口径**：全部数字取自 DEVLOG 既有回测记录（第 8/9/10/11 轮），无新回测；成交笔数由 总利润÷EV 推算（1w 46 笔与既有记录精确吻合，交叉验证了算术）；"非亏损率≠赚钱率"“手续费未计""1w 样本不足""参数已冻结不承诺继续优化"等诚实口径全部保留。AGENTS.md 头部注释新增 STRATEGY.md 指引（要求与回测记录保持同步、不得引入未验证数据）。

## 2026-08-25（第四轮）移动端响应式适配（手机访问 UI 优化）

- **背景**：用户计划在家用电脑部署、手机通过局域网/公网访问，问 UI 是否做了移动端优化。审计结论：CSS 无任何 `@media` 断点——360px 固定侧栏在 375px 手机上把图表挤到 ~15px、Header 单行必然溢出；另外发现 **`.macro-row` 固定列宽合计 ~414px，在桌面端 360px 侧栏里本来就溢出**（走势列被静默裁切）。
- **方案选型**：纯 CSS 断点 + 最小 TSX 改动（不加 JS 视口检测、不引 UI 框架，符合项目技术决策）。Header 需要拆分组才能干净地两行折叠，故重构 Header.tsx 结构为 `header-main`（品牌+搜索）/`header-controls`（周期+按钮+时间）两组，并把可隐藏文字包进 `.brand-text`/`.btn-label`/`.ws-label` span 供 CSS 控制——后续加 Header 按钮请沿用此约定。
- **改动**（App.css 断点 + Header.tsx 分组）：
  - **≤880px（平板竖屏/手机）**：`.main` 转纵向，图高 `clamp(280px, 46vh, 460px)`，侧栏全宽在下方内部滚动（sticky Tab 不受影响）；Header 基础样式加 `flex-wrap`，中等宽度自动折行不再裁切
  - **横屏矮视口（≤880px 且 landscape 且 max-height 500px）**：整页改为自然滚动（`.app height:auto`），图高 320px，避免图+侧栏挤进 ~375px 高度
  - **≤640px（手机）**：Header 纵向两组（第二组内部 wrap 成 2-3 小行）；刷新按钮只留 ⟳ 图标、预警只留 🔔+开关、更新时间隐藏"更新于"前缀；搜索框 110px、搜索下拉**右锚** `right:0`（防左锚溢出屏幕）、快捷币种/周期按钮紧凑化；toast 左右 8px 全宽；扫描弹窗近全屏（100dvh 回退 100vh）；侧栏/面板 padding 收紧
  - **iOS 细节**：≤640px 所有文本输入框 font-size 16px（<16px 会触发 Safari 聚焦页面缩放）
  - **顺手修复（桌面端也受益）**：`.macro-row` 从 7 固定列改为 `64px 1fr 48px 48px` 4 列自动两行（资产/最新/1D/30D 上行，相关/β/走势下行，sparkline 跨两列）——消除 360px 侧栏下的既有溢出
  - 图表自适应无需改动：ChartPanel 已有 `ResizeObserver → chart.resize()`，断点切换容器尺寸变化自动重绘
- **验证**：`npm.cmd run build` tsc 零错误；断点逻辑经桌面端窄窗口手动复核（375×667 / 390×844 / 768×1024 / 横屏 667×375 四档）
- **被否决方案**：JS `useMediaQuery` hook 条件渲染不同文案（多一层状态、闪烁风险，纯 CSS 足够）；Header 第二行 `overflow-x: auto` 横滑（扫描/预警按钮被藏出屏外，功能可发现性差）；把周期组挪进图表区（改动面大、与桌面布局分叉）

## 2026-08-25（第三轮）盈利扩展三杠杆（目标校准：扩大盈利）

- **背景**：用户把最高优先级目标校准为"扩大盈利"，要求检查系统还有什么可增强。审计结论：策略参数已冻结（11b 轮），利润公式 `P = EV × 笔数 × 遵循率` 中 EV 不可再调参，剩余杠杆是①机会数②遵循率③跨仓位管理——分别对应三个已实现功能。
- **杠杆① 机会捕捉（笔数↑）**：
  - **市场级计划观察器**（前端 `utils/alerts.ts` checkPlans + App 刷新时拉 `/api/scan`，仅预警铃铛开启时）：扫描结果中 hasPlan 标的出现**新计划或计划转向**→ toast + 浏览器通知（toast 点击切标的）；首个刷新周期静默播种（防通知风暴）、每标的 30min 冷却、当前标的不重复提醒（屏上已可见）。后端无改动（scan 已有 5min 服务端缓存，前端复用）
  - **当前标的挂单监控**（setPlan + checkPrice 内联）：价格回踩至计划入场区 ≤0.3×ATR → "挂单注意成交"；挂单窗口 fillBars 根到期 → "按纪律撤单/重估"（到期重臂=每窗口提醒一次）。关键设计：计划身份 key=symbol|interval|direction，**entry 随 ATR 漂移原地更新**不重置计时——否则每次刷新 entry 变动都会被当成新计划导致永不到期
- **杠杆② 组合分诊（跨仓位不漏管）**：`POST /api/portfolio/advise` 每仓位 `attention{level,text}`——danger（无止损/止损越强平价/超时间退出窗口）> warn（浮亏 ≤-1R 或逆势 |评分|≥25）> info（评分轻度反向）> ok（顺势）；评分走扫描器口径（200 根 full_analysis，无 MTF/衍生品——分诊用，精确动作看仓位面板）；rows 增 unrealizedR/barsHeld；items 汇总"N 个仓位需立即处理"。PortfolioPanel 按严重度排序 + attention chip（紧急/注意/偏逆，title 显示原因）
- **杠杆③ 遵循率成本可见化**：`GET /api/journal/stats` 增 `byExitReason{reason:{count,sumR,avgR,winRate}}`（按离场原因拆解盈亏来源——止损/保本跟踪/目标/时间/手动各贡献多少 R）与 `adherenceEv{followed,deviated:{count,sumR,avgR}}`（遵循 vs 偏离的均值差=**偏离成本 R/笔**，执行层优势是否兑现的直接度量）；JournalPanel 渲染偏离成本行（黄色高亮）+ 离场原因 R 拆解行
- **验证**：journal stats 用两笔测试交易（一笔止损 followed、一笔手动 followed）实测 byExitReason/adherenceEv 结构正确后删除还原（closed=0）；portfolio 三仓位实测（BTC 1 年 4h 仓→danger 超时退出、ETH 无止损→danger、SOL 顺势 +42→ok），danger 聚合 item 正确；tsc 零错误
- **诚实口径**：计划观察器的"新计划"推送是机会提醒非入场建议（是否下单仍按计划卡纪律）；attention 评分不含 MTF/衍生品上下文（UI tooltip 已标注）

## 2026-08-25（第二轮）仓位建议二次增强（第十一轮：提高收益/减少亏损）

- **背景**：用户要求在第十轮基础上"继续提升对已有仓位的策略和建议能力，以提高收益或减少亏损"。同时把开发日志从 AGENTS.md 抽离到本文件。
- **后端 `routers/position.py` 新增**（全部实测 200）：
  - **`action{level,text}` 最优先动作**：按优先级给用户**一个**当前最该执行的纪律步骤——缺止损(danger) > 止损越过强平价(danger) > 时间退出窗口已过(warn) > 证据转空且浮亏早离场(warn) > 保本/跟踪止盈执行(ok) > 按计划持有(info，附防守位)。避免用户在十几条建议里迷失——提高遵循率即提高实际收益
  - **`thesisState` 持仓证据状态**：评分(×2)/最新结构事件/MTF 多数/CVD 背离四源带符号合计 → strong(≥3)/intact(≥1)/weakened(≥-1)/broken(<-1)。描述性汇总非预测；前端 chip 着色
  - **`takeProfitLadder[]` 止盈参考阶梯**：盈利侧（越过入场价才算目标，adverse 侧位不是止盈参考——首版 bug：ETH 空头 entry 2400 时把 2490 的"区间高点"标成 +(-1.12)R，已修）候选位：未扫流动性池（含触碰次数）/VAH-VAL/POC/区间极值/决策卡关键位，0.3% 聚类去重按距离取前 4，每级附 distPct 与可锁 +R；盈利侧无位=真空区提示（盈利中："让利润奔跑，防守交给跟踪止盈"）
  - **入场质量**：scoreAtOpen 对照 PLAN_THRESHOLD——低于阈值提示"入场不在系统信号内，按防守型管理"；顺势且达阈值提示"入场质量良好"
  - **止损宽度校验**：<0.8×ATR=warn 偏紧（正常波动即可扫损，附建议位）；>3×ATR=info 偏宽（全额风险大、R 效率低）
  - **早离场启发式（减少亏损）**：浮亏 + thesisState=broken → warn"可考虑主动减仓/离场，不必等止损全额兑现"
  - **跟踪容忍收紧**：已过 +beR 且证据转弱/破裂时，建议把 trail 回撤容忍收紧一半（附收紧后的止盈价位）
  - **资金费率 carry（需 qty）**：每 8h 成本/收入金额 + 本地历史分位（history_stats.fundingPctl）+ 下次结算倒计时（UTC 00/08/16 边界）；支出且费率 ≥0.03% 时 warn"负 carry 侵蚀浮盈"。实测 BTC 0.0066%（91% 分位）支出、ETH 0.0030%（82% 分位）对空仓为收入
  - **高影响事件预警**：48h 内 events.json 高影响事件 → warn 建议提前降杠杆/收紧止损/减仓。`routers/calendar.py` 新增 `upcoming_events(from_ms, horizon_ms)`（事件时间按北京时区解析——events.json 中 CPI 20:30 即北京时间）
  - **MAE 展示修正**：全程未浮亏（最差时点仍为正）时显示"全程未出现浮亏（最差 +X.XXR）"而非误导性的"最大浮亏 +X"
- **前端**：PositionAdvice 类型扩展（action/thesisState/takeProfitLadder）；PositionPanel 顶部**动作横幅**（按 level 着色）+ 头部**证据状态 chip** + **止盈阶梯表**（价位/位置/距离/可锁 R）；SourceHint 同步更新；`npm.cmd run build` tsc 零错误
- **实测**：BTC 4h 老多仓（thesis=strong、action=时间退出、真空区提示、funding 支出项）；ETH 1h 逆势空仓（thesis=broken、action=早离场、阶梯只含 1900 附近池）；ETH 1h 盈利空仓（阶梯含区间高点 +8.05R 等 4 级）；热路径仍 ~4s

## 2026-08-25（第一轮）仓位分析增强（第十轮）

- **背景**：用户要求"加强对已开仓位的分析和建议"。此前 /api/position/advise 只做当前周期评分对照（无 MTF/衍生品组件，与决策卡口径不一致）+ R 阶梯管理。
- **后端 `routers/position.py` 重写**（全部实测 200，校验路径 400 正常）：
  - **评分同口径**：主窗口 400→500 根并补 prevDay/MTF（复用 `routers.analysis._mtf_context`）/资金费率+OI（`_derivatives_context`，币安实时→Gate.io 日线回退），与 /api/analysis 评分实测完全一致（BTC 4h 双端均 +50）
  - **开仓时点决策回放**（需 openedAt）：`scoreAtOpen`——仅用开仓前已收盘 K 线（end_time=开仓所在K线前一根）+当时已收盘高周期+当时已收盘日线衍生品（daily_rates，无前视）；评分漂移按持仓方向归一（±25 触发 ok/warn、±10 info）；开仓时即逆势入场提示
  - **持仓期间事件 `eventsSinceOpen[]`**：结构 BOS/CHoCH（按最新事件方向汇总——最新反向 CHoCH=warn、反转后回到持仓方向=ok（考虑 CHoCH 计数）、仅反向 BOS=info）、扫流动性事件（outcome 映射方向：broken=顺突破方向、reclaimed=反向）、Wyckoff spring/utad/sos；**持仓早于 500 根窗口时自动拉长覆盖窗口（≤3000 根）**重跑 full_analysis 取事件，barsHeld 用时间戳精确计算（实测 1 年 4h 仓位 2204 根、MFE +12.73R 正确）
  - **新增建议项**：高周期背景对照（全部相反=warn/分歧=info/支持=ok）；当前周期 CVD 背离对照；MFE/MAE 偏移行+盈利回吐 ≥0.5R 警告+深浮亏（≤-0.7R）后回升提示；结构止损参考（盈利中最近确认摆动低/高点，`levels.structureStop`）；止损紧贴未扫流动性池（≤0.5×ATR）插针风险+池外 0.3×ATR 建议；波动率压缩提示
  - 修复：MFE/MAE 的 `after` 窗口从 `openedAt-step` 改为 `bar_start`（对齐入场不再多算一根）
- **前端**：`PositionAdvice` 类型扩展（maeR/scoreNow/scoreAtOpen/eventsSinceOpen/structureStop）；PositionPanel 头部新增**评分漂移 chip**（开仓→当前，按持仓方向着色）与 **MFE/MAE chip**；新增「持仓期间事件」列表（时间+方向左边框着色）；SourceHint 更新说明；`npm.cmd run build` tsc 零错误
- **实测性能**：热路径 ~4s（含双份 MTF+回放上下文，kline_cache 命中）；无 openedAt 时 ~2s

## 2026-08-24 机构级升级（第九轮）+ 新因子回测（第 12 轮）+ 证伪清理（第 12b 轮）

### 第九轮：机构级升级（差距分析驱动的九个模块）

背景：对照机构交易助手标准做差距分析（数据广度/微观结构/组合风控/执行闭环四层缺口），用户拍板全部实现。网络重新探测结论变化见 AGENTS.md §6。

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
- `services/binance.py` 增 `get_mirror_json()`：白名单镜像端点（depth/ticker24hr），沿用主机冷却机制；二次修订：`get_depth(allow_mirror=)`（订单簿链序用）、`get_ticker24h()`（fapi→镜像 fallback 表）、`host_status()`（/api/sources 诊断用）

**前端新增**（tsc 零错误、代理全链路 200）：
- **侧栏三 Tab**（决策/市场数据/交易，sticky 标签栏，localStorage `coinlens.tab` 持久化）——解决面板数量爆炸后的导航问题
- `Header` 新增「⚡扫描」按钮 → `ScannerModal`（880px 弹窗：市场宽度统计、过滤框、评分排序表、CVD 背离列、计划标记列、当前标的高亮，点击行切换标的，Esc 关闭）
- `MacroPanel`（7 资产×涨跌/30日相关/beta/迷你走势线，相关性着色 |corr|≥0.7 加粗）、`OnchainPanel`（算力/内存池/费率/活跃地址/难度周期）、`OrderBookPanel`（四档深度带双向失衡条+大单墙列表）、`LiquidationPanel`（24h 多空清算+烈度百分位+48h 双向柱图+杠杆强平位表）
- `PortfolioPanel`（读取 localStorage 全部已存仓位聚合：净/总敞口/风险预算/集中度/相关性警告/beta；权益输入持久化 `coinlens.equity`）、`JournalPanel`（「按当前计划记录」一键快照 tradePlan/手动记录/平仓并复盘（含计划重放结果展示）/统计条（胜率/合计R/遵循率）/删除）
- `DerivativesPanel` 增强：OI/资金费率附分位数（"92%分位"）、大户持仓比卡片、期权卡新增 25Δ RR 与 Max Pain
- 数据流：refreshData 统一刷新 analysis+derivatives+backtest+orderbook+liquidations+onchain+macro（后两个服务端有 10/30min 缓存，实际不重复拉）；symbol 切换时 orderbook/liquidations 重置重拉

**实测性能**：orderbook 1.7s / liquidations 首拉 3s（含回填等待）/ onchain 3.9s / macro 首拉 15.2s→缓存后 <1s / scan 首拉 15 标的 6.9s→缓存 <1s / 冷启动符号 derivatives+backfill 36.8s（一次性，之后 5.8s 热路径）

**诚实口径声明（UI tooltip 均已标注）**：清算数据是 Gate.io 统计口径而非逐笔 feed；订单簿是快照而非流；估算强平位未计维持保证金；链上实体标签数据（净流入/稳定币）不可达——不编造

### 第 12 轮：新因子（衍生品历史+宏观）回测与决策集成 + K 线回放

- **背景**：第九轮机构级升级加入了衍生品持久化（derivs.db：Gate.io contract_stats 1d×1000，2023-11 起）与宏观联动（macro.db：Yahoo 5 年日线）。用户要求把新因子纳入决策并按利润优先回测，另支持"点一根 K 线看当时决策"的回放
- **数据回填**：本机 derivs.db/macro.db 首次回填（BTC/ETH/SOL 各 1000 日线 + 7 宏观序列×5 年）；修复 derivs_store `_newest_ts` 秒/ms 单位 bug（原先每次调用都误判过期触发回填）
- **工具**：`tests/profit3_factors.py`（因子诊断 + 9 门控扫描）+ `tests/profit3_weights.py`（评分组件权重模拟——因子增量直接叠加到缓存记录的 score 上重导出计划方向，无需重算记录）。日志 `tests/p3_*.log`
- **因子诊断（A+B 折，前瞻 24 根收益 IC）**：1d 上有真实方向信号——OI 百分位 IC -0.137（低 OI 反向做多）、散户多空比百分位 IC -0.113（反向）、大户多空比 IC +0.137（同向）、VIX IC +0.120（买恐慌）；4h 全部 |IC|<0.10；1w 大户 +0.139 但 n≈200
- **门控扫描（容量约束，th=10，A+B 选/盲测验证）**：9 个门控（funding/LSR 拥挤单边跳过、清算烈度、VIX>28、DXY/NDX 风险规避）**全部低于无门控基线**（4h 基线盲测 +285.1R vs 最佳门控 +282.7R；1d +44.2R vs +43.9R；1w +9.2R）——与第 11 轮"门控减少利润"结论一致，跨数据维度成立
- **权重模拟（6 格：inc/full/half/top-only/crowd-only/macro-only）**：1d 上 A+B 直接选定 incumbent（因子有害）；4h full +2.2% 盲测（低于预登记 10% 验收线）；1w top-only +19% 相对但仅 36 笔。**结论：因子不进评分、不做门控，以展示型 factorContext 集成**（与 FVG/图表形态的零权重模式一致）
- **生产集成**：`decision.build_summary` 新增 derivs_ctx/macro_ctx 参数 → `summary.factorContext`（衍生品分位/大户多空比/清算烈度/VIX/美元/纳指，UI 因子条展示+极端状态零权重 reason）；**既有 funding/OI 加权组件经 derivs_store 获得真实数据**（本网络币安不可达时此前一直为 null，Gate.io 回退补全——设计补全而非新组件，权重沿用早期校准）。`macro.factor_context()`/`derivs_store.factor_context()` 提供时点因子值（只用已收盘行，无前视）
- **K 线回放**：`/api/analysis?asOf=<ms>`——K 线截断到该根（含）、MTF 只用已收盘高周期 K 线（ts≤asOf+step-htStep）、衍生品/宏观因子取当时已收盘日线的时点值、prevDay 前一交易日；前端 ChartPanel `onCandleBarClick`（payload `{dataIndex,data:{current}}`）→ App 拉 asOf 分析 → 决策 Tab 顶部蓝色回放横幅（时间+"返回实时"）、决策/交易计划/MTF/量价分布/SMC 标注全部切换为回放时点、图表蓝色竖带标记回放 K 线、点其他 K 线移动回放点、切币/切周期自动退出
- **数据源变化注意**：本机 fapi.binance.com 现可达（此前 451 区域封锁）——kline_cache 近端 K 线自动切换为官方期货数据（1w 历史起点 2019-09=365 根，短于镜像现货 471 根；4h/1d 的 MTF 上下文价格亦切换），第 11 轮的记录缓存已在 11b 期间按混合数据重算，本轮基线（4h 盲测 +285.1R/1d +44.2R/1w +9.2R）与第 11 轮发布值（+288.1/+46.6/+21.2R）有小幅差异——内部对照有效，历史值仅作参考

### 第 12b 轮：回测证伪因子清理

用户要求"无用的因子从 UI 和代码删除并记录"。删除清单（全部有回测证据支撑）：
- **第 12 轮新因子 factorContext**（资金费率分位/大户多空/散户分位/清算烈度/VIX/美元5日/纳指5日 chips）——门控与权重模拟双双未达利润优先验收线（见第 12 轮）；UI FactorStrip、decision.py derivs_ctx/macro_ctx 参数、macro.factor_context()、derivs_store 因子分位计算全部删除
- **图表形态 + K 线形态**（双顶/头肩/三角/吞没/PinBar/晨星/暮星）——第 6 轮起零权重、多轮归因一致为负；patterns.py 模块删除、engine 不再计算、响应 patterns 字段与图表标记移除
- **FVG/扫流动性/偏离度（extension）决策分支**——零权重死代码（FVG 第 6 轮起、sweep 第 2 轮起、extension 一直为 0）；WEIGHTS 键同步清理

保留清单（非决策因子或有真实权重）：FVG 检测（交易计划入场区锚点，第 11 轮验证几何 zones=OB+FVG 的一部分）；扫流动性事件（预警引擎与图表"扫↑/扫↓"标记，事件信息而非方向因子）；funding/OI 加权组件（权重 10/8 与 10/6 来自早期校准，Gate.io 日线回退经 **derivs_store.daily_rates()** 保留）；Wyckoff（权重 6/8）；DerivativesPanel/MacroPanel（市场数据面板，非决策因子，分位数展示来自 history_stats）

- **顺手修复回放前视 bug**：asOf 模式此前会取 fapi **实时**资金费率/OI（早晨 -60 与午后 -45 评分差异即此症状——实时 OI 波动混入历史决策）；现在回放只用当时已收盘日线（daily_rates(asOf)），与回测口径一致
- **评分不变性验证**：ETH 1h=23/4h=60/1d=52、BTC 1w=36 删除前后完全一致（删除项全部零权重或纯展示）——生产决策零变化；SMC 单测通过；tsc 零错误
- **证据保留**：profit3_factors.py / profit3_weights.py 回测工具与 p3_*.log 日志保留在库中作为删除依据；decision.py 模块 docstring 记录完整裁定理由

## 2026-08-22 历史数据 + 利润优先优化（第三轮 + 第 10/11/11b 轮）

### 第三轮：历史数据与回测采样增强

- **K 线本地磁盘缓存**（用户要求："以前的数据加载过之后是不会变化了"）：`services/kline_cache.py`，SQLite（backend/data/cache/klines.db，WAL，PK=symbol+interval+ts）。请求 `limit` 根、终点 `end_time` 的窗口时：先查缓存并做**连续性检测**（顶部紧邻 end_time、内部间隔恒等于 interval 步长），全覆盖则直接返回；否则并发 4 路向币安分页拉缺失段（每页 1000 根按时间窗平铺，页间无重叠/空洞）→ 入库 → 重读合并窗口。end_time=None（最新）时顶页永远实拉（新 K 线会变）并回种缓存。实测：同参数 4.0s→0.036s；回测预热后图表翻页 0.03s。注意：缓存不区分合约/现货源，冲突时后写覆盖（价差极小，可接受）
- **回测采样窗口扩到 2 年**（用户要求"起码要 2 年内的数据去采样"；此前 limit≤1000，1h 只有 ~25 天）：`/api/backtest` 固定拉最近 2 年（当时 15m≈70k/1h≈17.5k/4h≈4.4k/1d≈730 根，limit 参数废弃、前端已同步去掉）。实测 BTC 1h：样本 390→17,302，首拉 14.5s（18 页并发）/缓存后 1.5s；ETH 15m 首拉 46s（70 页）/缓存后 5.5s（纯计算）。2 年大样本 IC≈0.02、胜率≈52%——与"技术面方向预测上限 ~60%"的诚实结论一致
- **图表左移分页修复 + 按钮**：klinecharts v10 语义勘误（重要，曾搞反）：`forward`=加载更早历史（前插，滚动到最左缘触发）、`backward`=加载更新数据（后插，滚动到最右缘触发）；`callback(data, more)` 的 more 传 false 会把两个方向全关掉。图表左上角「⟵ 加载更多历史」按钮（scrollToDataIndex(0) 触发库内部分页），每页 500 根；历史 K 线走 kline_cache 磁盘缓存，回测预热过的窗口翻页 0.03s

### 第 10 轮：利润优先重校准 4h/1d（用户要求"利润第一、胜率其次，不做超短线/日内"）

- **工具**：`tests/profit_sweep.py`（144 格网格：depth{0.75,1.0}×stop{2.0,2.5,3.0}×mgmt{plain,be05,scale05,scale10}×tgt{1.5,2.0,3.0}×texit{24,96}；**目标函数=总利润（成交单 R 值之和）优先，其次 EV、胜率**；约束 filled≥120(4h)/30(1d)、成交率≥25%、EV≥+0.05R）+ `tests/profit_eval.py`（补充定向评估）。日志 `tests/profit_4h.log`、`tests/profit_1d.log`。数据走 kline_cache 2 年窗口；**MTF 上下文只用已收盘的更高周期 K 线**（消除半根 K 线前视）
- **15m 周期同轮移除**（前端 INTERVALS / 后端 ALLOWED_INTERVALS、MTF_MAP、TWO_YEARS_BARS、STEP_MS 全部清理，interval=15m 返回 400）
- **4h 结果**（2829 决策点，2024-11~2026-08）：利润最优族 = depth 1.0 / stop 2.0 / **scale05**（+0.5R 减半+保本）/ tgt 3R / texit 24。走样本盲测 **B +42.4R（胜率 76.3%，EV +0.139R）/ C +38.1R（胜率 80.6%，EV +0.132R）**，均高于基线（98% 非亏损版 +32.2/+33.7R，EV +0.10R）；抽稀 1/2→83.5%、1/4→85.0% 稳定；盲测 C 分币种 BTC 83.5%/ETH 82.2%/SOL 75.6% 全正
- **1d 结果**（480 决策点，样本薄）：总利润最大化的"裸止损 3R 目标"格（plain tgt3 te96）样本内 +65.8R 但盲测 C **-12.4R**（20 笔、胜率 20%）→ 按协议拒绝（彩票型参数）。稳定族 scale05/stop2.0 补充盲测（profit_eval.py）：**B +9.2R（胜率 80.4%，EV +0.201R）/ C +10.0R（胜率 100%，EV +0.357R）**，三币种 B+C 全正（BTC 87%/ETH 85%/SOL 92%）——约为基线利润的 3 倍
- **LOSO 提示**：留一币种时"总利润最大化"选择器不稳定（2 币种子集会选中 plain 彩票格，盲测 BTC/SOL 转亏）；scale05 族本身跨币种、跨折、跨周期稳定——**生产采用统一 swing 几何**而非逐币种选择
- **生产选型（PLAN_GEOMETRY 按周期分化）**：1h 保留第 9 轮 be10_scale（保本优先 98%）；**4h/1d 统一 depth 1.0 / stop 2.0 / +0.5R 减半+保本 / 目标 3R / texit 24 根(4h)、96 根(1d)**，rr=1.75。TradePlanCard 按 interval 显示几何与回测口径（4h/1d 文案=利润优先盲测数据）
- 诚实提示（UI tooltip 已标注）：未计手续费/滑点（净 EV 再减 0.03~0.06R）；1d 盲测折仅 30~80 笔成交；激进高利润格盲测转亏已被拒绝——总利润上限受"参数稳健性"约束

### 第 11 轮：多轮循环优化（用户要求"7 小时循环、利润优先、4h/1d/1w、防过拟合、无法提升则说明原因并停止"）

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

### 第 11b 轮：扩展样本验证（已按用户要求取消）与优化正式终止

- **用户对 1w 46 笔盲测的质疑**（合理）：样本 <100 则数据不可靠。成因：① 币安周线数据上限（BTC/ETH 471 根、SOL 315 根——2017/2020 上市）；② 容量约束串行执行（周线一仓占坑最长 24 周≈5.5 个月）；③ 盲测仅占时序后 60%（无约束口径其实 201 笔）。**1d/1w 总利润低于 4h 的成因**：总利润=EV×笔数，EV 其实 1w 最高（+0.46R>4h +0.32R），差距全在 K 线根数（4h 6570 vs 1d 1460 vs 1w 471）与机会频率——提高高周期利润的杠杆是增加标的数而非改参数
- **11b 尝试与取消**：方案=几何只用 BTC/ETH/SOL 调参，新增 10 个长历史主流币（BNB/XRP/ADA/DOGE/LINK/LTC/DOT/TRX/ETC/BCH）做纯样本外验证（工具 `tests/profit2_oos.py` 已写、跑了一半）——**用户否决**：不同币走势完全不一样，新币验证对 BTC/ETH/SOL 交易无意义。已停止（镜像限流导致 LINK/LTC 拉取失败也是实际问题）
- **后续方向（用户提出，未排期）**：加入美股或与加密相关的标的（MSTR/COIN/NDX 等）扩充样本与市场维度。数据源实测（2026-08-22）：`query1.finance.yahoo.com` 可达但**连续请求会被限流**（先 404 后连接重置，需加间隔/重试/换 query2 主机）；`stooq.com` CSV 接口当时稳定可达（2026-08-24 复测已下线：带 UA 也返回 HTML）。落地时需做限流控制与独立适配层
- **最终结论固化（生产配置即第 11 轮结果，不再迭代）**：PLAN_GEOMETRY 1h=(0.75,2.5,0.1R,0.75R,96)/4h=(0.75,1.2,0.5R,trail0.5,48,fill18)/1d=(0.75,1.5,0.5R,trail0.5,24,fill9)/1w=(0.75,1.5,0.5R,trail0.75,24,fill8)；PLAN_THRESHOLD 4h/1d/1w=10、1h=25；容量口径盲测 4h +288.1R/1d +46.6R/1w +21.2R（1w 样本不足是已声明的局限）。**新增功能替代继续优化**：仓位建议（POST /api/position/advise + PositionPanel）把校准几何用于实盘持仓管理——这是回测结论向用户价值转化的路径

## 2026-08-21 首版上线 + 决策引擎回测校准

### 首版 MVP（第一轮）

- 后端 FastAPI + 前端 React/klinecharts v10 全链路跑通：SMC 引擎单测通过（BOS/OB/FVG 识别正确）；/api/analysis BTC/ETH/SOL 均 200；边界校验 400 正常
- **failover**（官方域名优先，失败才走镜像）：`services/binance.py` 主源 4s 短超时 → 失败标记主机冷却 300s（后续请求快速短路）→ 仅 klines/exchangeInfo 回退镜像；合约专属接口无镜像→路由层置 null。实测：首次请求 6s（含探测），冷却期内 1.8s
- 前后端联调验证：/api 代理链路 health/analysis/derivatives 全通；首次 analysis 6s 含 failover 探测，冷却后 1.3s；启动脚本 start-backend.ps1 / start-frontend.ps1；用户浏览器验收反馈"UI 不错"
- 首次 git commit `8ca7bd7`（62 文件：后端+前端+测试+文档+启动脚本；.venv/node_modules/dist/缓存已忽略）
- npm 网络问题解法（D:\Work\Coin 企业网）：DNS 把 registry 域名劫持到 127.0.0.1，`frontend/tools/dns-override.cjs`（Node --require 钩子映射回真实 IP）解决；首次在 D:\Work\Coin 跑通全链路（BTC 1h score=60 bullish）
- 当时数据模式：derivatives 30s 轮询 + WS 断开 60s 轮询降级（后被 2026-08-22 数据模式取代，见下）

### 第二轮：用户体验修复

- **流动性池标注**：`$$$已扫` 改为 `买侧流动性·已扫 / 卖侧流动性`（$$$ 是 SMC 圈"挂在 swing 高低点的挂单流动性"黑话，用户反馈看不懂）
- **图表历史分页**：初始加载 500 根（1h≈21 天），向左滚动自动加载更早历史（当时的语义踩坑记录见第三轮勘误）
- **SourceHint tooltip**：原 absolute 定位在 `.sidebar{overflow-y:auto}` 内被裁剪（视觉上像被 K 线图盖住）→ 改为 React Portal + `position:fixed` + z-index 1000，自动视口内夹紧（左右贴边、底部翻转向上），150ms 延迟关闭使链接可点击
- **币安官方 API 连通性复测**：fapi/api.binance.com 返回 HTTP 451（连通但被币安区域封锁，出口 IP 所在地区受限），镜像 data-api.binance.vision 正常——当时结论：本网络只能走镜像（现货数据），合约专属接口不可达（2026-08-24 复测 fapi 已可达，见第 12 轮注）

### 数据模式切换（2026-08-22，用户要求）

不使用实时推送（即使能连上 WS 也不用；币安 WS 模块删除）。统一为**手动刷新按钮 + 每 5 分钟自动刷新**（开关持久化 localStorage `coinlens.autoRefresh`），刷新时一次性重拉 analysis/derivatives/backtest 并把新 K 线尾部原地同步进图表（`syncBars`：时间戳等于最后一根→更新、更大→追加、更旧→忽略，不重置视图）。原 derivatives 30s 轮询、WS 断开 60s 轮询降级均已移除。同日移除 15m 周期。

### 决策引擎回测校准记录

工具：`backend/tests/backtest_decision.py`（随机时间点决策回测 + 防过拟合协议：数据按时间 60/40 切分，前 60% 调参（IS）、后 40% 盲测（OOS）；门控候选集预先限定、粗粒度；记录缓存 `_bt_cache.pkl` 按引擎源码哈希自动失效）。

**回测-修复循环过程（7 轮）**：
1. ETH 单币 100 点：1D 胜率 46%（低于抛硬币）→ 组件归因发现 CVD 背离最强（IC+0.38）、扫流动性事件有 bug（历史陈旧事件一直计入）→ 修复近因过滤
2. 修复后 1D 55.9%/1W 60.3%，但扫流动性仍负贡献 → 权重清零（保留展示/预警）
3. 三币种合并 300 点：方向门控 IS 最优 d1 门控 OOS 仅 38% → 暴露样本不足问题
4. 发现高周期 CVD 背离全为 0：重采样丢 takerBuy 字段 → 修复；交易计划模拟发现"区域中位入场"成交率仅 7% → 不可执行
5. 4h/1d CVD 背离解锁，共振门控 IS 68.3%
6. 2 年数据 ×3 币种 450 点大样本：**证伪 CVD 共振**（OOS 42.9%），所有组件长期方向胜率 50% 附近——技术面预测周线方向本质上接近抛硬币（最佳简单门控 55~61%，随市况波动）
7. 转向可执行性：交易计划改为回踩 0.5×ATR 限价入场 + 1×ATR 止损 + 1:1/1:2 目标 + **+0.5R 保本移损**管理

**最终验证结果（2 年 × BTC/ETH/SOL × 450 决策点，60/40 时间切分）**：

| 指标 | IS（调参期） | OOS（盲测期） |
|---|---|---|
| 保本管理计划·非亏损率（盈利+保本离场） | **83.3%**（n=114） | **88.9%**（n=72；BTC 96%/ETH 83%/SOL 88%） |
| 其中：盈利 / 保本 / 全损 | 32.5% / 50.9% / 16.7% | 40.3% / 48.6% / 11.1% |
| 每笔期望（成交后） | +0.16R | +0.29R |
| 限价成交率（24 根内） | 89% | ~85% |
| 复合评分方向胜率（1D/1W） | ~48-51% | ~52-61%（市况依赖） |

**结论与产品定位修正**：
- **方向胜率 >80% 不可达**（诚实结论）：任何技术组件在 2 年大样本上都无法稳定超过 ~61%；此前 8 个月小样本上的"CVD 66%"是特定时段运气（大样本上回落到 49%）
- **可靠优势在执行层**：回踩入场 + 保本移损的计划管理使"非亏损率"达 83~89%（IS/OOS 一致、跨币种一致、期望为正）——这是给用户的核心承诺，已在 TradePlanCard 展示并附方法论 tooltip
- 评分体系已按大样本归因重校：CVD 保持中等权重（1W IC+0.16）、结构/EMA/共振保留、FVG/图表形态/K线形态/偏离度/扫流动性**零权重**（多轮归因一致为负，仅保留展示）；趋势市/震荡市权重分化保留
- 防过拟合要点：OOS 只看一次、门控候选预定义、分币种交叉验证、refuse 了"目标位小于止损"的胜率虚高方案

**1000 点/币种大样本复核（用户要求的再验证）**：
- 规模：3000 决策点（每币 1000，~16h 间距，2 年窗口），IS 1800 / OOS 1200；工具 `tests/backtest_decision.py --points 1000`，日志 `tests/bt1000.log`，独立性校验 `tests/thin_analysis.py`
- **保本管理计划非亏损率**：IS **82.3%**（fill 761）／ OOS **82.2%**（fill 529；BTC 79.7% / ETH 85.2% / SOL 81.7%）；EV：IS +0.11R / OOS +0.15R 每笔
- **抽稀独立性校验（OOS）**：1/1（~16h）82.2% → 1/4（~2.6天）87.2% → 1/8（~5.3天）86.5% → 1/16（~10.5天）79.4%（n=34）——去除自相关后仍稳定在 ~80-87% 区间，结论对采样密度不敏感
- 方向门控 1W 抽稀后 57.5~60.1% 稳定；150 点时"OOS 88.9%"系小样本偏乐观（72 个成交），大样本中心估计修正为 **~82%**；产品 UI 文案已同步改为 "~82%（抽稀 79~87%）"
- 大样本下各组件方向 IC 全部 |IC|<0.16、胜率 48-53%——再次确认技术面方向预测上限 ~60% 的诚实结论

**第 8 轮：走样本前向校准，非亏损率 ~91%（用户要求 90% 目标）**：
- 工具：`tests/plan_sweep.py`（日志 `tests/sweep.log`）。协议：时序 40%/30%/30% 三段（A 调参 → B 盲测 → A+B 重调 → C 盲测）；36 格粗粒度几何网格 + 4 门控；**EV 硬约束 ≥ +0.05R**（防"目标缩水刷胜率"）；超时单按市价结算（比旧口径更严格）
- **两阶段独立选出同一配置**（稳健平台而非刀锋拟合）：回踩 0.75×ATR / 止损 1.5×ATR / 保本触发 +0.25R / 目标 0.75R+1.5R / 96 根时间退出
- 结果：A 93.1% → **B 盲测 94.9%**（n=352）；A+B 重调 93.8% → **C 盲测 91.0%**（n=356，EV +0.148R）；C 段抽稀 91.2%/90.4%；分币种 92.3%/90.4%/90.1%（全 >90%）
- 参数面单调平滑（止损 1.5>1.0、保本 0.25>0.35>0.5 方向一致符合直觉），非噪声挑选；代价是单笔盈利缩小（EV 从 +0.25R 降到 +0.15R），用期望换确定性
- 该几何后被第 9 轮的 be10_scale 取代（胜率 91%→98%，EV +0.15R→+0.09R）

**第 9 轮：分批止盈边界外移，非亏损率 ~98%（用户要求向 99% 推进）**：
- **先验数学**：纯限价成交下 P(全损) ≥ f/(1+f)（f=保本触发 R 数），99% 需 f≈0.01 → EV 塌向 0。合法的边界外移手段 = **分批止盈**（+f×R 出半仓锁利润 + 保本，剩余仓位跑目标）
- 工具：`tests/plan_sweep2.py`（96 格网格：depth{0.75,1.0}×stop{1.5,2,2.5}×be{0.05,0.1,0.15,0.25}×tgt{0.75,1.0}×scaleout{off,on}，EV≥+0.05R + 成交率≥40% 约束）+ `tests/loso_validation.py`（留一币种交叉验证）。日志 `tests/sweep2.log`
- **走样本结果**：样本内 99.2%（stop=2.5/be=0.05 家族两阶段稳定占优），但 ranging 门控盲测 EV 跌破约束线（B 段 +0.040、ETH -0.016）→ **按协议拒绝该门控**（n=158 小样本陷阱）
- **LOSO 结果**（因 B/C 段已被查看，改用跨资产泛化检验，更严格）：三折独立选几何，盲测 BTC **97.2%**/EV+0.096、ETH **98.0%**/EV+0.075、SOL **98.4%**/EV+0.069——全部高于 EV 约束线
- **诚实结论：~98% 是 EV>0 约束下的上限**。99%+ 参数在样本内存在但盲测期望贴零，被 EV 约束拒绝
- **生产选型 be10_scale**：0.75 回踩 / 2.5 止损 / +0.1R 减半仓+保本 / 剩余半仓 0.75R / 96 根退出。与最高胜率格（be05）胜率差仅 1-2 笔/400（噪音），EV 更优且约束 margin 翻倍（LOSO 最差 +0.081 vs +0.069）
- 已知局限：回测未计手续费/滑点（maker 入场约 0.02%），净 EV 约再减 0.03~0.06R——UI tooltip 已如实标注

### P0/P1/P2 分析/策略增强详情（全部完成）

**P0 —— 直接提升结论质量**：
1. **多周期共振（MTF）**：`/api/analysis` 内联 `mtf` 字段（当时 15m→[1h,4h]、1h→[4h,1d]、4h→[1d]），并行拉取高周期 K 线（60s 缓存）跑同一套引擎取 bias/score；决策加共振权重 ±15；前端 MtfBar 显示周期芯片 + 共振/冲突标签。实测 BTC 1h：4h:bullish:67 + 1d:bullish:19 → aligned
2. **Regime 分化评分**：decision.py 按 ADX≥25 分支两套权重（趋势市：结构±35/EMA±10/OB±8/RSI 不减分；震荡市：结构±12/OB±12/RSI 反向±10/溢价折价±12），reasons 文案标注"趋势市权重/震荡市权重"
3. **OB/FVG 质量过滤**：OB 仅保留 displacement≥0.8×ATR 起源（突破段相对 ATR 强度），quality=displacement(60%)+放量(40%)+回踩守住 bonus(15)，按质量排序取前 10；FVG 过滤 <0.1×ATR 噪音 gap，quality 同理；决策权重按质量缩放（0.5~1.0 倍）
4. **CVD/主动买卖盘**：利用 K 线自带 takerBuy 字段（fapi 与镜像都有，索引 9）计算每根 delta=2×takerBuy-volume 累加成 CVD 曲线；检测近 30 根价格/CVD 背离（价涨 CVD 跌=虚假突破），决策 ±8/±10；前端新增 CVD 副图 pane

**P1 —— 补全逻辑链**：
5. **Wyckoff 阶段识别**：`wyckoff.py`——近 60 根区间检测（宽度≤3.5×ATR 为区间），价格位置+成交量萎缩判吸筹/派发；事件：Spring（刺破下沿收回）/UTAD（上冲回落）/SOS（放量突破）；阶段进决策权重 ±6/±8，事件进 reasons；图表上以青色文字标注
6. **K线/图表形态**：实现后于 2026-08-24 第 12b 轮按回测结论删除（零权重、多轮归因一致为负；patterns.py 模块删除）——详见 12b 轮
7. **假突破事件流**：`smc.sweepEvents[]`——每个流动性池被扫时记录（时间/价位/方向/outcome：reclaimed 收回=反转信号 vs broken 突破=延续信号，3~5 根内判定）；决策权重后清零（第 2 轮起负贡献），保留预警与图表"扫↑/扫↓"标注
8. **动态成交量分布**：`developing_poc_series()`——累计 bin 矩阵向量化计算滚动 300 根 POC 序列（120 采样点）；前端图表画金色虚线 POC 轨迹 + 面板显示"动态POC"
9. **Gate.io 衍生品恢复**：`services/gateio.py`——futures tickers(funding)+contract_stats(OI 历史+lsr_account 多空比+lsr_taker 买卖比)+contracts 规格(乘数)；derivatives 路由币安失败自动回退（source 字段标记来源）；options_snapshot 初版：最近到期月 ATM IV（delta 最接近±0.5 的 call+put 均值）+ PCR（持仓量比）（2026-08-24 第九轮重写扩展 RR25/期限结构/Max Pain）
10. **波动率状态机**：ATR 百分位+布林带宽百分位（各 200 根回看）→ compressed/normal/expanded + squeeze（带宽<20%分位且 ATR<30%分位）；决策加"波动收缩期，等突破"提示（weight 0 信息项）；DecisionCard 显示波动状态标签

**P2 —— 可信度与风控闭环**：
11. **评分回测证伪**：`/api/backtest`——walk-forward 复算轻量评分（结构/EMA/RSI/溢价折价/CVD 背离，regime 分化权重），统计与未来 8 根收益的 Spearman IC + |score|≥15 方向胜率。实测 BTC 1h（当时小样本）：IC=0.31、胜率 79.7%（n=59；大样本复核后回落到 IC≈0.02/52%，见上）；DecisionCard 底部"历史验证"行 + 方法论 tooltip
12. **交易计划输出**：`summary.tradePlan`——|score|≥25 时生成（后经第 10/11 轮演进为按周期分化的校准几何，见 AGENTS.md §4 契约）
13. **事件日历**：`/api/calendar` + `backend/data/events.json`（2026 剩余 FOMC + CPI 模板，含 Fed 官网链接 tooltip；网络封锁无法自动拉取，本地手动维护）；前端 CalendarPanel
14. **多周期预警**：`utils/alerts.ts` AlertEngine——关键位触及（0.2% 容差，10 分钟冷却）/新 CHoCH/新扫流动性事件 → 浏览器 Notification + 站内 toast（右下角，点击消失）；Header 铃铛开关（请求通知权限）

"""AUDIT ONLY (2026-08-28): machine verification of STRATEGY.md consistency.

Step 3 of the doc-audit round: every backtest number in STRATEGY.md must be
traceable to BACKTEST.md (per task protocol) and consistent with the machine
reruns in tests/audit_baseline.log / tests/audit_fee_dir.log /
tests/audit_doc_compound.log. Read-only: parses logs + docs, recomputes
derived figures, prints a verdict table. No file is modified.

Run: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/audit_doc_strategy.py
"""
import os
import re
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(ROOT, "backend")
TESTS = os.path.join(BACKEND, "tests")


def read(p):
    with open(p, "rb") as f:
        raw = f.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # PowerShell Tee-Object → UTF-16
        return raw.decode("utf-16")
    return raw.decode("utf-8")


strategy = read(os.path.join(ROOT, "STRATEGY.md"))
backtest = read(os.path.join(ROOT, "BACKTEST.md"))
baseline = read(os.path.join(TESTS, "audit_baseline.log"))
feedir = read(os.path.join(TESTS, "audit_fee_dir.log"))
compound = read(os.path.join(TESTS, "audit_doc_compound.log"))
decision = read(os.path.join(BACKEND, "services", "analysis", "decision.py"))

rows = []


def verdict(cid, loc, claim, doc_val, machine_val, ok, evidence, note=""):
    rows.append((cid, loc, claim, doc_val, machine_val, ok, evidence, note))


# ---------------------------------------------------------------- machine values
def m(pat, text, cast=float, src=""):
    mm = re.search(pat, text)
    if not mm:
        raise SystemExit(f"parse fail: {pat} in {src}")
    return cast(mm.group(1))


# audit_baseline.log — T1 full-window NEW-R13 totals
t1_1h_n = m(r"T1-NEW-R13\] 1h 四币合计: 成交=(\d+)", baseline, int, "baseline")
t1_4h_n = m(r"T1-NEW-R13\] 4h 四币合计: 成交=(\d+)", baseline, int, "baseline")
t1_1d_n = m(r"T1-NEW-R13\] 1d 四币合计: 成交=(\d+)", baseline, int, "baseline")
t1_4h_dd = m(r"T1-NEW-R13\] 4h 四币合计: .*DD=([\d.]+)R", baseline, float, "baseline")
t1_1d_dd = m(r"T1-NEW-R13\] 1d 四币合计: .*DD=([\d.]+)R", baseline, float, "baseline")
# T7 blind fold-C NEW
t7_1h_n = m(r"T7-FC-NEW-R13\] 1h: 成交=(\d+)", baseline, int, "baseline")
t7_4h_n = m(r"T7-FC-NEW-R13\] 4h: 成交=(\d+)", baseline, int, "baseline")
t7_1d_n = m(r"T7-FC-NEW-R13\] 1d: 成交=(\d+)", baseline, int, "baseline")
t7_4h_nonloss = m(r"T7-FC-NEW-R13\] 4h: .*非亏损=([\d.]+)%", baseline, float, "baseline")
t7_4h_ev = m(r"T7-FC-NEW-R13\] 4h: .*EV=\+([\d.]+)R", baseline, float, "baseline")
t7_4h_dd = m(r"T7-FC-NEW-R13\] 4h: .*DD=([\d.]+)R", baseline, float, "baseline")
t7_1d_dd = m(r"T7-FC-NEW-R13\] 1d: .*DD=([\d.]+)R", baseline, float, "baseline")
t7_4h_old_ev = m(r"T7-FC-OLD-R11\] 4h: .*EV=\+([\d.]+)R", baseline, float, "baseline")
t7_4h_old_nonloss = m(r"T7-FC-OLD-R11\] 4h: .*非亏损=([\d.]+)%", baseline, float, "baseline")
# audit_fee_dir.log — direction split
t5_1h_lo_ev = m(r"T5-1h-long\].*EV=\+([\d.]+)R", feedir, float, "feedir")
t5_1h_sh_ev = m(r"T5-1h-short\].*EV=\+([\d.]+)R", feedir, float, "feedir")
t5_4h_lo_ev = m(r"T5-4h-long\].*EV=\+([\d.]+)R", feedir, float, "feedir")
t5_4h_sh_ev = m(r"T5-4h-short\].*EV=\+([\d.]+)R", feedir, float, "feedir")
t5_1d_lo_ev = m(r"T5-1d-long\].*EV=\+([\d.]+)R", feedir, float, "feedir")
t5_1d_sh_ev = m(r"T5-1d-short\].*EV=\+([\d.]+)R", feedir, float, "feedir")
t5_1h_sh_pct = m(r"T5-1h-short\].*占比=([\d.]+)%", feedir, float, "feedir")
t5_4h_sh_pct = m(r"T5-4h-short\].*占比=([\d.]+)%", feedir, float, "feedir")
# audit_fee_dir.log — fee sensitivity
t4_fee007 = m(r"双边0\.07%≈([\d.]+)R/笔", feedir, float, "feedir")
t4_fee010 = m(r"双边0\.10%≈([\d.]+)R/笔", feedir, float, "feedir")
t4_4h_ev_007 = m(r"T4-4h\] 双边0\.07%.*EV=\+([\d.]+)R", feedir, float, "feedir")
t4_4h_ev_010 = m(r"T4-4h\] 双边0\.10%.*EV=\+([\d.]+)R", feedir, float, "feedir")
t4_4h_ev_gross = m(r"T4-4h\] gross.*EV=\+([\d.]+)R", feedir, float, "feedir")
t4_1h_ev_gross = m(r"T4-1h\] gross.*EV=\+([\d.]+)R", feedir, float, "feedir")
t4_1h_ev_010 = m(r"T4-1h\] 双边0\.10%.*EV=\+([\d.]+)R", feedir, float, "feedir")
t4_1h_ev_012 = m(r"T4-1h\] 双边0\.12%.*EV=\+([\d.]+)R", feedir, float, "feedir")
t4_1h_nl_gross = m(r"T4-1h\] gross.*非亏损=([\d.]+)%", feedir, float, "feedir")
t4_1h_nl_010 = m(r"T4-1h\] 双边0\.10%.*非亏损=([\d.]+)%", feedir, float, "feedir")
t4_cross = m(r"T4-cross\] 1h/4h 总利润交叉点=双边 ([\d.]+)%（单边 ([\d.]+)%）", feedir, str, "feedir")
t4_cross_b = re.search(r"T4-cross\] 1h/4h 总利润交叉点=双边 ([\d.]+)%（单边 ([\d.]+)%）", feedir)
cross_dbl, cross_sgl = float(t4_cross_b.group(1)), float(t4_cross_b.group(2))
# compound log — window years
span_years = m(r"数据窗口: .*（([\d.]+) 年）", compound, float, "compound")

# decision.py — production geometry ground truth
geo = dict(re.findall(r'"(1h|4h|1d|1w)": \(([\d., None]+)\)', decision))
thres = dict(re.findall(r'"(1h|4h|1d|1w)": (\d+),', decision))

# ---------------------------------------------------------------- derived
wks = span_years * 365.25 / 7
freq_1h = t1_1h_n / 4 / wks
freq_4h = t1_4h_n / 4 / wks
freq_1d_wk = t1_1d_n / 4 / wks  # trades per coin per week
fee_eat_1h = (t4_1h_ev_gross - t4_1h_ev_010) / t4_1h_ev_gross * 100
fee_eat_4h_010 = (t4_4h_ev_gross - t4_4h_ev_010) / t4_4h_ev_gross * 100
fee_eat_4h_012 = None
mm = re.search(r"T4-4h\] 双边0\.12%.*EV=\+([\d.]+)R", feedir)
if mm:
    fee_eat_4h_012 = (t4_4h_ev_gross - float(mm.group(1))) / t4_4h_ev_gross * 100
loss_share_4h = 100 - t7_4h_nonloss

print("=" * 110)
print("A. STRATEGY.md 回测数字逐条核对（对照 BACKTEST.md 出处 + 机器复算）")
print("=" * 110)

# --- S 系列：§2 核心思想
verdict("S1", "§0/§2", "方向准确率≈五成多/测不过60%", "五成多 / <60%",
        "LTC 46.8~48.3%（BACKTEST §2）", "✓", "BACKTEST.md §2 line 37；AGENTS 已知局限 ~60%",
        "定性声称，BACKTEST 有支撑")
verdict("S2", "§2", "留下组件方向胜率 50%~61%", "50%~61%",
        "BACKTEST 无此数字", "⚠", "BACKTEST.md 全文检索无 50%~61%",
        "DEVLOG 调参记录类数字，BACKTEST 未收录")
verdict("S3", "§2", "4h 盲测段 ~79% 单子不亏钱", "约79%",
        f"{t7_4h_nonloss:.1f}%（T7-FC-NEW 4h）", "✓", "BACKTEST §4.2=79.4%；audit_baseline.log:125 80.0%",
        "BACKTEST 值 79.4% 与文档一致；复算 80.0%（切分漂移）")
verdict("S4", "§2", "~21% 亏满 1R", "约21%",
        f"{loss_share_4h:.1f}%（100−非亏损）", "✓", "派生值 100−79.4=20.6%", "")
verdict("S5", "§2", "4h 盲测段平均每笔 +0.45R", "+0.45R",
        f"+{t7_4h_ev:.3f}R（T7-FC-NEW 4h）", "✓", "BACKTEST §4.2=+0.446R；audit_baseline.log:125 +0.452R", "")
verdict("S6", "§2", "重校准前 4h EV +0.31R", "+0.31R",
        f"+{t7_4h_old_ev:.3f}R（T7-FC-OLD 4h 盲测段）", "⚠", "BACKTEST.md 无 +0.31（§4.1 全时段旧=+0.302R）；audit_baseline.log:130",
        "BACKTEST 未收录盲测段旧 EV；机器值 +0.314 支持文档")
verdict("S7", "§2", "非亏损率 87%→79%", "87%→79%",
        f"{t7_4h_old_nonloss:.1f}%→{t7_4h_nonloss:.1f}%", "✓", "audit_baseline.log:125,130（87.2→80.0）；BACKTEST 仅有 79.4%",
        "旧值 87% 在 BACKTEST 无出处但机器支持")
# --- §4 盲测表
verdict("S8", "§4表 1h行", "盲测段成交笔数", "4232",
        f"{t7_1h_n}（T7-FC-NEW 1h）", "✗", "audit_baseline.log:113 成交=4206",
        "BACKTEST §4.2 无笔数字段；偏差 −0.6%（切分点随记录集尾部漂移）")
verdict("S9", "§4表 1h行", "盲测段 97.1% / +0.169R / +714.0R", "97.1%/+0.169R/+714.0R",
        "96.8% / +0.161R / +677.7R", "✓*", "BACKTEST §4.2 同值 ✓；audit_baseline.log:113（切分漂移 −5.1%）",
        "与 BACKTEST 一致；与当前缓存复算有漂移")
verdict("S10", "§4表 1h行", "盲测段最大回撤", "4.8R",
        "DD=3.1R（T7-FC-NEW 1h 复算）", "⚠", "BACKTEST §4.2 代价段 '1h 回撤 4.0→4.8R' ✓ 有出处；audit_baseline.log:113 复算 3.1R",
        "4.8R 系原始运行值；当前缓存复算 3.1R")
verdict("S11", "§4表 4h行", "1054 / 79.4% / +0.446R / +470.6R / 5.0R", "同行",
        f"{t7_4h_n} / {t7_4h_nonloss:.1f}% / +{t7_4h_ev:.3f}R / +476.4R / {t7_4h_dd:.1f}R",
        "✓", "BACKTEST §4.2 同值；audit_baseline.log:125（笔数 1054 精确一致）", "")
verdict("S12", "§4表 1d行", "172 / 86.0% / +0.311R / +53.6R", "同行",
        f"{t7_1d_n} / 91.6% / +0.411R / +68.3R", "✓*", "BACKTEST §4.2 同值 ✓；audit_baseline.log:137 复算偏离较大",
        "BACKTEST 一致；复算差异见第一部分第 6 项（harness/切分口径）")
verdict("S13", "§4表 1d行", "盲测段最大回撤", "3.6R",
        f"{t7_1d_dd:.1f}R（T7-FC-NEW 1d）；全时段 T1 1d DD={t1_1d_dd:.1f}R", "✗",
        "BACKTEST.md 无 1d 盲测 DD；audit_baseline.log:137 盲测复算 2.0R、:87 全时段 3.6R",
        "疑似把全时段 DD 当盲测段 DD（张冠李戴）")
verdict("S14", "§4表 1w行", "64笔 / ~94% / +0.46R / +29.6R / 2.0R", "同行",
        "无出处", "✗", "BACKTEST §4.2 1w='—'（预登记排除）；1w 缓存已过期无法复算",
        "STRATEGY 独有、BACKTEST 之外的回测数字，且不可机器验证")
# --- §4 方向拆分
verdict("S15", "§4", "1h 多/空 EV +0.167/+0.164R", "+0.167/+0.164",
        f"+{t5_1h_lo_ev:.3f}/+{t5_1h_sh_ev:.3f}（T5 复算）", "✓*", "BACKTEST 无出处（DEVLOG 第二十五轮）；audit_fee_dir.log:64-65",
        "复算 +0.170/+0.161，差 ≤0.006（缓存尾部 +3 天）")
verdict("S16", "§4", "4h 多/空 EV +0.440/+0.415R", "+0.440/+0.415",
        f"+{t5_4h_lo_ev:.3f}/+{t5_4h_sh_ev:.3f}（T5 复算）", "✓*", "audit_fee_dir.log:66-67（+0.445/+0.412）", "同上")
verdict("S17", "§4", "做空占比 57~61%", "57~61%",
        f"1h={t5_1h_sh_pct:.1f}% 4h={t5_4h_sh_pct:.1f}%（T5 复算）", "✗", "audit_fee_dir.log:64-67",
        "DEVLOG 原值 1h=61.2%（8511/13904）在区间内；复算 1h=68.4% 超区间上沿——harness 口径差异，需解释")
verdict("S18", "§4", "1d 多头略优 +0.45 vs +0.37R", "+0.45/+0.37",
        f"+{t5_1d_lo_ev:.3f}/+{t5_1d_sh_ev:.3f}（T5 复算）", "✓*", "audit_fee_dir.log:68-69（+0.456/+0.375）",
        "BACKTEST 无出处（DEVLOG 第二十五轮）")
# --- §4 换算与频率
verdict("S19", "§4", "4h 每笔 +0.45R ≈ +45 USDT（1% 风险）", "+45 USDT",
        "0.446×100 = 44.6 ≈ 45", "✓", "算术核验", "")
verdict("S20", "§4", "最大回撤 5R ≈ −5%", "−5%",
        f"4h 盲测 DD={t7_4h_dd:.1f}R", "✓", "BACKTEST §4.2 代价段 5.7→5.0R；audit_baseline.log:125", "")
verdict("S21", "§4", "频率 1h 每币每周 ~13 次", "~13",
        f"{freq_1h:.1f}（{t1_1h_n}/4币/{span_years}年）", "✓", "由 audit_baseline.log:27 + audit_doc_compound.log 窗口年复算", "")
verdict("S22", "§4", "频率 4h 每币每周 ~3 次", "~3",
        f"{freq_4h:.1f}", "✓", "同上", "")
verdict("S23", "§4", "频率 1d 每币两周 1 次", "两周1次",
        f"{freq_1d_wk:.2f}/周（≈每 {1/freq_1d_wk:.1f} 周 1 次）", "✓", "同上", "")
verdict("S24", "§4", "频率 1w 每币一年 ~5 次", "~5/年",
        "不可复算（1w 缓存过期）", "⚠", "BACKTEST §4.1 1w=105 笔（5 年窗四币合计，窗长不明）",
        "未验证")
# --- §4/§5 费用
verdict("S25", "§4", "双边~0.07% ≈0.04R/笔", "≈0.04R",
        f"{t4_fee007:.3f}R/笔（T4）", "✓", "audit_fee_dir.log:34；BACKTEST 无出处（DEVLOG 第二十三轮）", "")
verdict("S26", "§4", "双边 0.10% ≈0.06R/笔", "≈0.06R",
        f"{t4_fee010:.3f}R/笔（T4 中位口径）；敏感度 0.062R（T4-be）", "✓", "audit_fee_dir.log:34,57", "")
verdict("S27", "§4", "4h 净期望约 +0.38~0.40R/笔", "+0.38~0.40R",
        f"+{t4_4h_ev_010:.3f}（双边0.10%）~ +{t4_4h_ev_007:.3f}（双边0.07%）", "✗",
        "audit_fee_dir.log:46-47（+0.366~+0.384）；+0.40 无场景支撑",
        "上沿虚高；且与 STRATEGY §5 自己的 '+0.37R' 不一致")
verdict("S28", "§5.4", "4h +0.43R 被费用吃掉 14~17%（净期望约 +0.37R）", "14~17% / +0.37R",
        f"毛 EV +{t4_4h_ev_gross:.3f}；吃掉 {fee_eat_4h_010:.1f}%（0.10%）~ {fee_eat_4h_012:.1f}%（0.12%）；净 +{t4_4h_ev_010:.3f}",
        "✓", "audit_fee_dir.log:43,47-48", "")
verdict("S29", "§5.4", "1h +0.17R 被吃掉四成左右", "~40%",
        f"{fee_eat_1h:.1f}%（双边0.10%）", "✓", "audit_fee_dir.log:35,39（EV 0.164→0.101 = 38.4%）", "")
verdict("S30", "§5.4", "1h 97% 非亏损率费后降到约 80%", "97%→~80%",
        f"{t4_1h_nl_gross:.1f}%→{t4_1h_nl_010:.1f}%（双边0.10%）", "✓", "audit_fee_dir.log:35,39", "")
verdict("S31", "§5.4", "单边 0.06% 时 1h 总利润被 4h 反超", "单边0.06%",
        f"交叉点 双边{cross_dbl}%（单边{cross_sgl}%）", "✓", "audit_fee_dir.log:59", "")
# --- §5 其他
verdict("S32", "§5.5", "1w 5 年四币共 105 笔", "105",
        "105（BACKTEST §4.1 同值）", "✓", "BACKTEST.md:82", "")
verdict("S33", "§5.6", "Top10 旧几何 10/10 盈利", "10/10",
        "BACKTEST §3 同值", "✓", "BACKTEST.md:62（本轮未复算 Top10）", "")
# --- 窗口表述
verdict("S34", "文头 line4", "数据期间 2024-11 ~ 2026-08", "2024-11~2026-08",
        "2021-08~2026-08（5 年窗）", "✗", "BACKTEST §4 背景 line 71；audit_baseline.log:1 数据自 2021-08-29",
        "窗口起点写错（陈旧残留）")
verdict("S35", "§4 特别提醒", "回测窗口（2024-2026）", "2024-2026",
        "2021-08~2026-08", "✗", "同上", "窗口写错")

for r in rows:
    print(f"[{r[5]:>2}] {r[0]:<4} {r[1]:<10} {r[2]:<28} 文档={r[3]:<22} 机器={r[4]:<40} 证据={r[6]}")
    if r[7]:
        print(f"      ↳ {r[7]}")

# ---------------------------------------------------------------- geometry table vs production
print()
print("=" * 110)
print("B. STRATEGY §3 几何表 vs 生产 PLAN_GEOMETRY（decision.py:74-79）")
print("=" * 110)
print(f"decision.py PLAN_GEOMETRY: {geo}")
print(f"decision.py PLAN_THRESHOLD: {thres}")
expected = {
    # tf: (回踩, 止损, 保本, 目标/跟踪, texit, fill)
    "1h": ("0.5×ATR", "2.0×ATR", "+0.15R", "目标 0.5R 固定", "96 根", "24（生产值；STRATEGY 表写作 '—'）"),
    "4h": ("0.75×ATR", "1.0×ATR", "+0.75R", "跟踪 0.35R", "48 根", "18"),
    "1d": ("1.0×ATR", "1.2×ATR", "+0.5R", "跟踪 0.35R", "12 根", "9"),
    "1w": ("0.75×ATR", "1.5×ATR", "+0.5R", "跟踪 0.75R", "24 根", "8"),
}
for tf, exp in expected.items():
    print(f"  {tf}: 生产 {geo.get(tf)} | 期望 {exp}")
print("  → STRATEGY 表与生产唯一不符：1h 挂单有效期写作 '—'，生产 fill_bars=24（BACKTEST §4.2 '/24 根' ✓）")

# ---------------------------------------------------------------- generic scan
print()
print("=" * 110)
print("C. STRATEGY.md 数字全文扫描：凡 BACKTEST.md 无同值者列出（'不得有 BACKTEST 之外回测数字'机检）")
print("=" * 110)
# 非回测语义的白名单（端口/阈值/风控线/算术示例/日期等）
ignore_patterns = [
    r"^-100$", r"^\+100$", r"^15$", r"^25$", r"^10$",  # 评分阈值
    r"^1%$", r"^3%$", r"^6%$",  # 风控线
    r"^5$",  # 每5分钟
    r"^100$", r"^10,?000$", r"^500$", r"^45$",  # 换算示例
    r"^202[1-6](-\d{2})?(-\d{2})?$", r"^08-2[258]$",  # 日期
    r"^60%$", r"^50%$",  # 定性区间
    r"^2$", r"^3$", r"^4$",  # 序数
]
num_re = re.compile(r"[+~×]?-?\d[\d,]*(?:\.\d+)?(?:R|%|×|亿|万| USDT| 根| 笔| 天| 周| 个月| 年| 次)?")
seen = {}
for ln, line in enumerate(strategy.splitlines(), 1):
    if line.strip().startswith("|---") or not line.strip():
        continue
    for tok in num_re.findall(line):
        tok_n = tok.strip().lstrip("+~×")
        if not tok_n or len(tok_n.rstrip("R%×亿万 根笔天周个月年次USDT").strip()) == 0:
            continue
        if any(re.fullmatch(p, tok_n) for p in ignore_patterns):
            continue
        # 只关心带单位/量纲的回测语义数字
        if not re.search(r"(R|%|×|亿|万|笔|根|USDT)$", tok_n):
            continue
        seen.setdefault(tok_n, []).append(ln)
absent = {k: v for k, v in seen.items() if k not in backtest}
print(f"扫描到回测语义数字 {len(seen)} 个去重；BACKTEST.md 中无同值者 {len(absent)} 个：")
for k, v in sorted(absent.items(), key=lambda x: x[1][0]):
    print(f"  {k:<14} STRATEGY 行 {v[:4]}")
print()
print("注：白名单过滤了阈值/风控线/算术示例/日期；上表含 DEVLOG 直引数字（方向拆分/费用轮）与无出处数字，")
print("    逐条定性见 A 节 verdict。")
print()
print("[audit_doc_strategy] done")

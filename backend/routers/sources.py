"""Data-source diagnostics: priority chains + live host reachability.

The app never hardcodes "which network am I in" — every data type resolves
through an ordered source chain at runtime, and failed hosts enter a
cooldown so subsequent calls fail fast to the next source. This endpoint
reports the configured order and the current host state so users can see
which source is actually serving them in any environment.
"""
from fastapi import APIRouter

from services import binance, sysproxy

router = APIRouter(prefix="/api")

CHAINS = {
    "klines": {
        "order": ["binance_fapi(官方合约)", "binance_fapi_via_sysproxy(官方合约·系统代理)", "binance_spot_mirror(现货镜像)"],
        "note": "官方 4s 短超时探测，失败后若系统代理（VPN）可用则走代理再试，仍失败进入 300s 冷却后走镜像；K线/exchangeInfo 每次刷新都会先试官方",
    },
    "orderbook": {
        "order": ["binance_fapi_depth(合约盘)", "binance_fapi_depth_via_sysproxy(合约盘·系统代理)", "gateio_perp(合约聚合盘)", "binance_spot_mirror(现货盘)"],
        "note": "按盘口深度优先级排序；哪个可达用哪个，响应 source 字段标注实际来源",
    },
    "derivatives": {
        "order": ["binance_fapi(合约统计)", "binance_fapi_via_sysproxy(合约统计·系统代理)", "gateio(futures tickers+contract_stats)"],
        "note": "币安合约优先（直连或系统代理）；任一成功即返回，字段级降级（不可达的接口置 null）",
    },
    "derivs_history": {
        "order": ["gateio_contract_stats(1d×1000+1h×720，含清算USD)", "binance_futures_data(OI/费率/多空比，无清算)"],
        "note": "本地 derivs.db 持久化，多源按列合并；分位数上下文自动适配可用源",
    },
    "liquidations": {
        "order": ["gateio_contract_stats(long/short_liq_usd)"],
        "note": "唯一免费清算聚合源；不可达时多空清算置空（不编造），杠杆强平位仍可用",
    },
    "onchain": {
        "order": ["mempool.space(费率/内存池/难度)", "blockchain.info charts(算力/地址)"],
        "note": "字段级降级：单源失败只影响对应字段",
    },
    "macro": {
        "order": ["yahoo_chart(直连，浏览器UA+1.6s限速+双主机轮换)", "yahoo_chart_via_sysproxy(系统代理)"],
        "note": "直连 403 时若系统代理（VPN）可用则走代理；全路由 403 进入 15 分钟快速失败窗并服务本地缓存（Stooq CSV 端点 2026-08 复测已服务端下线）",
    },
}


@router.get("/sources")
async def get_sources():
    return {
        "chains": CHAINS,
        "hostStatus": binance.host_status(),
        "systemProxy": sysproxy.status(),
        "note": "所有数据源按优先级链在运行时探测选择；失败主机冷却 300s 后自动重试。"
                "系统代理（VPN）作为币安/Yahoo 直连失败后的中间链路，自动探测注册表配置，"
                "代理不可用不影响既有降级链。在任何网络环境下无需改代码：官方可达则用官方，"
                "不可达自动回退。",
    }

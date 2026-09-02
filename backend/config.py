# 首选币安官方合约 API；不可达时由 services/binance.py 自动回退到镜像
BINANCE_FAPI = "https://fapi.binance.com"

# 行情镜像（官方域名不可达时的 K 线/交易对列表回退源）
BINANCE_SPOT_MIRROR = "https://data-api.binance.vision"

# 合约测试网（免费假钱环境，executor 自动交易先行验证用）
BINANCE_FAPI_TESTNET = "https://testnet.binancefuture.com"

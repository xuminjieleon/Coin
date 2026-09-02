from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services import executor
from services.executor import EXEC_INTERVALS

router = APIRouter(prefix="/api")

# f 阶梯审计（第五十一轮）：1h+4h RUIN@17.5%。paper/testnet 是沙盒，允许
# 用户确认的 15% 假钱口径；实盘硬闸 ≤3，突破须显式重开一轮拍板。
RISK_PCT_CAP = 15.0
LIVE_RISK_CAP = 3.0


class ExecutorConfig(BaseModel):
    enabled: bool | None = None
    testnet: bool | None = None
    dryRun: bool | None = None
    confirmLive: bool | None = None
    apiKey: str | None = Field(default=None, min_length=1, max_length=128)
    apiSecret: str | None = Field(default=None, min_length=1, max_length=128)
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=20)
    intervals: list[str] | None = Field(default=None, min_length=1, max_length=4)
    riskPct: float | None = Field(default=None, ge=0.1, le=RISK_PCT_CAP)
    leverage: float | None = Field(default=None, ge=1, le=20)
    maxConcurrent: float | None = Field(default=None, ge=1, le=20)
    maxNotionalPctPer: float | None = Field(default=None, ge=5, le=500)
    maxGrossNotionalPct: float | None = Field(default=None, ge=20, le=2000)
    dailyLossLimitR: float | None = Field(default=None, ge=0, le=50)
    equityUsd: float | None = Field(default=None, ge=100, le=100_000_000)
    postOnlyEntry: bool | None = None
    pushEvents: bool | None = None


class PanicRequest(BaseModel):
    confirm: str


def _validate(cfg: ExecutorConfig) -> dict:
    patch = cfg.model_dump(exclude_unset=True)
    if patch.get("intervals") is not None:
        bad = [i for i in patch["intervals"] if i not in EXEC_INTERVALS]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"intervals 仅支持 {sorted(EXEC_INTERVALS)}；收到 {bad}")
    if patch.get("symbols") is not None:
        patch["symbols"] = [s.strip().upper() for s in patch["symbols"] if s and s.strip()]
        if not patch["symbols"]:
            raise HTTPException(status_code=400, detail="symbols 不能为空")
    return patch


@router.get("/executor")
async def get_executor_status():
    return executor.status()


@router.post("/executor")
async def update_executor_config(cfg: ExecutorConfig):
    patch = _validate(cfg)
    # W1 fix: validate against the POST-MERGE mode, never the patch shape —
    # a riskPct raise while live, or split arming/raise requests, cannot
    # bypass the live hard gate anymore.
    before = executor.status()
    before_live = (not before["dryRun"]) and (not before["testnet"])
    after_dry = patch.get("dryRun", before["dryRun"])
    after_tn = patch.get("testnet", before["testnet"])
    after_live = (after_dry is False) and (after_tn is False)
    if after_live:
        after_risk = patch.get("riskPct", before["riskPct"])
        if after_risk > LIVE_RISK_CAP:
            raise HTTPException(
                status_code=400,
                detail=f"实盘 riskPct 硬闸 ≤{LIVE_RISK_CAP:g}（f 阶梯审计：1h+4h RUIN@17.5%、"
                       f"DD≤10~20% 对应 f≤1~2%）；当前 {after_risk:g}。"
                       f"先在测试网看过高 f 的实际回撤再议")
        keys_ok = bool(patch.get("apiKey")) or before["keysSet"]
        if not keys_ok:
            raise HTTPException(status_code=400, detail="上实盘前必须先配置 API key")
        if not before_live and not patch.get("confirmLive"):
            raise HTTPException(
                status_code=400,
                detail="进入实盘需本请求携带 confirmLive=true（三重确认不因历史状态失效）")
    if patch.get("equityUsd") is not None and after_dry is not True:
        raise HTTPException(status_code=400,
                            detail="equityUsd 仅模拟盘可设（实网定仓基数=交易所钱包余额）")
    try:
        await executor.update_config(patch)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return executor.status()


@router.post("/executor/test")
async def test_executor():
    result = await executor.test_connection()
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "连接失败")
    return result


@router.post("/executor/panic")
async def executor_panic(req: PanicRequest):
    if req.confirm != "PANIC":
        raise HTTPException(status_code=400, detail="需 body {\"confirm\": \"PANIC\"} 确认")
    return await executor.panic()

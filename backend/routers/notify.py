from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services import notify, notifier
from services.analysis.context import ALLOWED_INTERVALS

router = APIRouter(prefix="/api")


class NotifyConfig(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    channel: str | None = None
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=10)
    interval: str | None = None
    token: str | None = Field(default=None, min_length=1, max_length=64)
    wecomKey: str | None = Field(default=None, min_length=1, max_length=256)


@router.get("/notify")
async def get_notify_status():
    return notifier.status()


@router.post("/notify")
async def update_notify_config(cfg: NotifyConfig):
    if cfg.mode is not None and cfg.mode not in notifier.MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {sorted(notifier.MODES)}")
    if cfg.channel is not None and cfg.channel not in notify.CHANNELS:
        raise HTTPException(status_code=400, detail=f"channel must be one of {sorted(notify.CHANNELS)}")
    if cfg.interval is not None and cfg.interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(ALLOWED_INTERVALS)}")
    symbols = None
    if cfg.symbols is not None:
        symbols = [s.strip().upper() for s in cfg.symbols if s and s.strip()]
        if not symbols:
            raise HTTPException(status_code=400, detail="symbols must not be empty")
    notifier.update_config(
        enabled=cfg.enabled,
        mode=cfg.mode,
        channel=cfg.channel,
        symbols=symbols,
        interval=cfg.interval,
        token=cfg.token,
        wecom_key=cfg.wecomKey,
    )
    return notifier.status()


@router.post("/notify/test")
async def test_notify():
    if not notifier.current_token():
        raise HTTPException(status_code=400, detail=notifier.credential_hint())
    title = f"CoinLens 测试 {datetime.now().strftime('%m-%d %H:%M')}"
    content = (
        "推送通道正常。\n\n"
        "每小时整点后 5 分钟将按当前模式推送交易计划信号；"
        "消息中的入场/止损/止盈均为计划挂单参考价，"
        "开仓后请以 App「我的仓位」的管理位为准。"
    )
    ok, error = await notifier.send_now(title, content)
    notifier.record_test_push(title, ok, error)
    return {"ok": ok, "error": error or None}

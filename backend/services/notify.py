"""WeChat push channels.

Two channels, dispatched by notifier config:
- "pushplus": https://www.pushplus.plus — free 200 msgs/day, requires
  real-name verification on their site; messages arrive in WeChat proper
  (official account). 905 = unverified account, 903 = bad token.
- "wecom": WeCom (企业微信) group robot webhook — free, unlimited (20
  msgs/min per robot), no real-name wall; messages arrive in the WeCom app.

Both send markdown, validate the business code in the response body, retry
once on network exceptions (never on business errors), and never raise.
"""

import asyncio

import httpx

PUSHPLUS_URL = "https://www.pushplus.plus/send"
WECOM_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

CHANNELS = {"pushplus", "wecom"}

_TIMEOUT = httpx.Timeout(15.0)
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _retry_once(post):
    """Call post(); on network exception retry once after 3s. Returns
    (ok, error). Business-code errors are returned as-is without retry."""
    try:
        return await post()
    except Exception as first_err:
        await asyncio.sleep(3)
        try:
            return await post()
        except Exception as retry_err:
            return False, f"{type(retry_err).__name__}: {retry_err} (retry after: {first_err})"


async def _post_pushplus(token: str, title: str, content: str) -> tuple[bool, str]:
    client = await _get_client()
    resp = await client.post(
        PUSHPLUS_URL,
        json={"token": token, "title": title, "content": content, "template": "markdown"},
    )
    resp.raise_for_status()
    data = resp.json()
    # PushPlus returns HTTP 200 with a business code in the body
    if data.get("code") == 200:
        return True, ""
    return False, f"pushplus code={data.get('code')} msg={data.get('msg')}"


async def _post_wecom(key: str, title: str, content: str) -> tuple[bool, str]:
    client = await _get_client()
    # WeCom markdown has no title field — prepend it as a bold first line
    resp = await client.post(
        WECOM_WEBHOOK,
        params={"key": key},
        json={"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{content}"}},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") == 0:
        return True, ""
    return False, f"wecom errcode={data.get('errcode')} msg={data.get('errmsg')}"


async def send_pushplus(token: str, title: str, content: str) -> tuple[bool, str]:
    """Send one markdown message via PushPlus. Returns (ok, error)."""
    if not token:
        return False, "no pushplus token configured"
    return await _retry_once(lambda: _post_pushplus(token, title, content))


async def send_wecom(key: str, title: str, content: str) -> tuple[bool, str]:
    """Send one markdown message via a WeCom group robot webhook key."""
    if not key:
        return False, "no wecom webhook key configured"
    return await _retry_once(lambda: _post_wecom(key, title, content))


async def send_by_channel(channel: str, credential: str, title: str, content: str) -> tuple[bool, str]:
    if channel == "wecom":
        return await send_wecom(credential, title, content)
    return await send_pushplus(credential, title, content)

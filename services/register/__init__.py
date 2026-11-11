"""注册机核心（从 gpt-outlook-register 合并而来）。

本包包含**与 WebUI 无关**的注册核心逻辑：
    auth_flow.py     OpenAI 注册全流程（10 步）
    fingerprint.py   浏览器指纹表与 UA 推断
    http_client.py   带重试的 HTTP 会话
    sentinel.py      Sentinel Token 入口
    sentinel_quickjs.py  Node 子进程跑 OpenAI 真实 sdk.js 算 PoW
    mail_providers/  邮箱来源（outlook / cf_temp / icloud_relay / remail）
    sms_provider.py  接码（2FA）支持
    config.py        注册参数配置

与主项目的关系
--------------
注册成功后由 `services/register_service.py` 把凭证写入
chatgpt2api 的号池（AccountService），格式为
`access_token----proxy`（proxy 可为空）。

⚠️ sentinel 必须跑在**真实的 Node.js** 上：纯 Python 合成的 PoW 会被
服务端静默丢弃，导致 OTP 收不到。详见 sentinel_quickjs.py。
"""

from __future__ import annotations

from .config import Config
from .auth_flow import AuthFlow
from .sentinel import get_sentinel_token

__all__ = ["Config", "AuthFlow", "get_sentinel_token"]

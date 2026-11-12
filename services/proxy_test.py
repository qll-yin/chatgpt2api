"""代理连通性与 IPv6 出口检测。

为什么单独成模块
----------------
这两个操作都要**并发跑一批代理**，每个都要等网络往返（几秒到几十秒）。
放在 API 层会让路由变成一坨线程池代码，测试起来也麻烦。

检测方式沿用原注册机的做法 —— 复用真实注册流程用的
`create_http_session`（含 socks5 → socks5h 标准化、trust_env=False），
保证「测试正常」==「跑号能用」。

- 连通性：打 api.ipify.org，拿出口 IP 与延迟
- IPv6：打 cloudflare.com/cdn-cgi/trace，它同时返回 ip 和 ipv6 两个字段
  （ip 是 IPv4，ipv6 是 IPv6）→ ipv6 字段非空即说明该出口支持 IPv6

为什么关心 IPv6
--------------
IPv6 的地址空间比 IPv4 大几个数量级，同一个 /64 段里的"邻居"少得多 →
被 Cloudflare 按 IP 段批量拉黑的概率低得多。原注册机日志里 7 次 CF 403
全是 IPv4 出口。
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# 默认探测地址
IP_CHECK_URL = "https://api.ipify.org?format=json"
IPV6_CHECK_URL = "https://cloudflare.com/cdn-cgi/trace"


def _session_for(proxy: str):
    """建一个走指定代理的 session（复用注册流程的工厂）。"""
    from services.register.http_client import create_http_session

    return create_http_session(proxy=proxy)


def test_one(proxy: str, timeout: int = 8, test_url: str = IP_CHECK_URL) -> dict[str, Any]:
    """测单个代理的连通性。返回 {ok, latency_ms, ip, error}。"""
    started = time.perf_counter()
    try:
        sess = _session_for(proxy)
        resp = sess.get(test_url, timeout=timeout)
        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
        ip = ""
        try:
            ip = (resp.json() or {}).get("ip", "")
        except Exception:  # noqa: BLE001
            ip = (getattr(resp, "text", "") or "").strip()[:64]
        return {"ok": True, "latency_ms": latency, "ip": ip}
    except Exception as exc:  # noqa: BLE001
        latency = int((time.perf_counter() - started) * 1000)
        return {"ok": False, "latency_ms": latency, "error": str(exc)[:140]}


def test_ipv6_one(proxy: str, timeout: int = 10) -> dict[str, Any]:
    """测单个代理是否走 IPv6 出口。返回 {ok, ipv6, latency_ms, ip, ipv6_addr, error}。"""
    started = time.perf_counter()
    try:
        sess = _session_for(proxy)
        resp = sess.get(IPV6_CHECK_URL, timeout=timeout)
        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            return {
                "ok": False,
                "ipv6": False,
                "latency_ms": latency,
                "error": f"HTTP {resp.status_code}",
            }
        text = getattr(resp, "text", "") or ""
        m4 = re.search(r"^ip=(.*)$", text, re.M)
        m6 = re.search(r"^ipv6=(.*)$", text, re.M)
        ip4 = m4.group(1).strip() if m4 else ""
        ip6 = m6.group(1).strip() if m6 else ""
        return {
            "ok": True,
            "ipv6": bool(ip6),
            "latency_ms": latency,
            "ip": ip4,
            "ipv6_addr": ip6,
        }
    except Exception as exc:  # noqa: BLE001
        latency = int((time.perf_counter() - started) * 1000)
        return {"ok": False, "ipv6": False, "latency_ms": latency, "error": str(exc)[:140]}


def _clean(proxies: list[str]) -> list[str]:
    return [p.strip() for p in (proxies or []) if p and p.strip()]


def test_proxies(
    proxies: list[str], timeout: int = 8, test_url: str = IP_CHECK_URL
) -> dict[str, dict[str, Any]]:
    """并发测一批代理的连通性。返回 {proxy: result}。"""
    targets = _clean(proxies)
    if not targets:
        return {}
    timeout = max(1, min(int(timeout or 8), 60))
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(20, len(targets))) as ex:
        for proxy, res in zip(targets, ex.map(lambda p: test_one(p, timeout, test_url), targets)):
            results[proxy] = res
    return results


def test_ipv6(proxies: list[str], timeout: int = 10) -> dict[str, dict[str, Any]]:
    """并发测一批代理的 IPv6 支持情况，并把结果落库。

    返回 {proxy: result}。
    """
    from services.storage import register_store as store

    targets = _clean(proxies)
    if not targets:
        return {}
    timeout = max(3, min(int(timeout or 10), 60))
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(10, len(targets))) as ex:
        for proxy, res in zip(targets, ex.map(lambda p: test_ipv6_one(p, timeout), targets)):
            results[proxy] = res
            # 结果落库，前端表格直接展示
            try:
                store.set_proxy_ipv6_status(proxy, "ok" if res.get("ipv6") else "fail")
            except Exception:  # noqa: BLE001
                logger.debug("[proxy-test] 写 IPv6 状态失败: %s", proxy, exc_info=True)
    return results

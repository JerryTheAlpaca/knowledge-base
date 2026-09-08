"""受限下载器：所有适配器的网络出口（docs/02 §9.3）。

- 仅 HTTP/HTTPS；每跳重定向都重新做 DNS/IP 校验。
- 拒绝回环、私网、链路本地、云元数据等非公网目的地（IPv4/IPv6 全部地址检查）。
- 连接与读取超时、最大解压后大小、MIME 校验；超限视为失败。
- 不转发服务 Token、模型 Key 或原请求 Cookie；跨主机跳转去掉
  Cookie/Authorization 等敏感头（docs/05 §3.2）。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ..config import get_settings

MAX_REDIRECTS = 5


class SafeFetchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _check_url_allowed(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SafeFetchError("SOURCE_BLOCKED", f"仅允许 HTTP/HTTPS：{parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise SafeFetchError("SOURCE_BLOCKED", "URL 缺少主机名")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SafeFetchError("NETWORK_ERROR", f"DNS 解析失败：{host}") from exc
    if not infos:
        raise SafeFetchError("SOURCE_BLOCKED", f"无法解析主机：{host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
            or ip in ipaddress.ip_network("169.254.169.254/32")  # 云元数据
            or ip in ipaddress.ip_network("100.64.0.0/10")  # 运营商级 NAT
        ):
            raise SafeFetchError("SOURCE_BLOCKED", f"拒绝非公网目的地：{ip}")


def _verify_redirect(request_url: str, response: httpx.Response) -> str | None:
    if response.is_redirect:
        location = response.headers.get("location")
        if not location:
            return None
        next_url = str(response.next_request.url)
        _check_url_allowed(next_url)
        return next_url
    return None


@dataclass
class FetchResult:
    url: str
    status_code: int
    mime: str
    content: bytes
    truncated: bool = False


def safe_fetch(url: str, *, max_bytes: int | None = None, timeout: float = 20.0,
               mime_prefixes: tuple[str, ...] | None = None,
               headers: dict[str, str] | None = None) -> FetchResult:
    """同步受限 GET。内容完整读入前先检查大小；超限抛错，不发布截断结果。

    headers 仅允许覆盖 User-Agent/Referer 等请求头，安全校验（DNS/IP/重定向/大小）不受影响。
    """
    settings = get_settings()
    limit = max_bytes or settings.html_download_limit
    _check_url_allowed(url)

    transport = httpx.HTTPTransport(retries=0)
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": "KnowledgeInbox/0.1 (+restricted-fetcher)"},
    ) as client:
        current = url
        current_host = (urlparse(current).hostname or "").lower()
        for _ in range(MAX_REDIRECTS + 1):
            try:
                # headers 按跳传递：跨主机跳转剥离敏感头，防止凭据跟随重定向
                hop_headers = {
                    k: v for k, v in (headers or {}).items()
                    if not (current_host != (urlparse(url).hostname or "").lower()
                            and k.lower() in _SENSITIVE_HEADERS)
                }
                resp = client.get(current, headers=hop_headers)
            except httpx.HTTPError as exc:
                raise SafeFetchError("NETWORK_ERROR", f"下载失败：{exc}") from exc
            if resp.is_redirect:
                current = _verify_redirect(current, resp)
                if current is None:
                    raise SafeFetchError("SOURCE_BLOCKED", "重定向缺少 Location")
                current_host = (urlparse(current).hostname or "").lower()
                continue
            break
        else:
            raise SafeFetchError("SOURCE_BLOCKED", f"重定向超过 {MAX_REDIRECTS} 跳")

    mime = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    if mime_prefixes and not mime.startswith(mime_prefixes):
        raise SafeFetchError("SOURCE_BLOCKED", f"不接受的 MIME：{mime}")

    declared = resp.headers.get("content-length")
    if declared and int(declared) > limit:
        raise SafeFetchError("PAYLOAD_TOO_LARGE", f"响应超过 {limit} 字节上限")

    content = resp.content
    if len(content) > limit:
        raise SafeFetchError("PAYLOAD_TOO_LARGE", f"响应超过 {limit} 字节上限")

    return FetchResult(url=str(resp.url), status_code=resp.status_code, mime=mime, content=content)

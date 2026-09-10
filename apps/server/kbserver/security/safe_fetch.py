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

# httpx 0.28 起不再导出 _SENSITIVE_HEADERS；本地维护这份清单用于跨主机跳转剥离敏感头
_SENSITIVE_HEADERS = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
})


class SafeFetchError(Exception):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


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


@dataclass
class ProbeResult:
    """轻量探针结果：最终地址、状态、MIME、声明的 Content-Length、是否可 range。"""
    url: str
    status_code: int
    mime: str
    content_length: int | None
    accept_ranges: bool
    sample: bytes = b""


def probe_url(url: str, *, headers: dict[str, str] | None = None,
              sample_bytes: int = 64 * 1024, timeout: float = 15.0) -> ProbeResult:
    """只读取响应头与最多 sample_bytes 字节，用于判断直链是否音频（docs/13 §4.2）。

    与其他出口同规则：每跳重定向重新校验 DNS/IP，跨主机剥离敏感头；不落完整响应。
    """
    _check_url_allowed(url)
    transport = httpx.HTTPTransport(retries=0)
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout, connect=10.0, read=timeout),
        follow_redirects=False,
        headers={"User-Agent": "KnowledgeInbox/0.1 (+restricted-fetcher)"},
    ) as client:
        current = url
        current_host = (urlparse(current).hostname or "").lower()
        origin_host = current_host
        for _ in range(MAX_REDIRECTS + 1):
            hop_headers = dict(headers or {})
            if current_host != origin_host:
                hop_headers = {k: v for k, v in hop_headers.items()
                               if k.lower() not in _SENSITIVE_HEADERS}
            # 只要开头若干字节；服务器忽略 Range 时也最多读 sample_bytes
            hop_headers.setdefault("Range", f"bytes=0-{max(0, sample_bytes - 1)}")
            try:
                with client.stream("GET", current, headers=hop_headers) as resp:
                    if resp.is_redirect:
                        next_url = _verify_redirect(current, resp)
                        if next_url is None:
                            raise SafeFetchError("SOURCE_BLOCKED", "重定向缺少 Location")
                        current = next_url
                        current_host = (urlparse(current).hostname or "").lower()
                        continue
                    mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    declared = resp.headers.get("content-length")
                    try:
                        length = int(declared) if declared else None
                    except ValueError:
                        length = None
                    ranges = (resp.headers.get("accept-ranges", "").lower() == "bytes")
                    buf = bytearray()
                    for chunk in resp.iter_bytes(chunk_size=16 * 1024):
                        buf += chunk
                        if len(buf) >= sample_bytes:
                            break
                    return ProbeResult(
                        url=str(resp.url), status_code=resp.status_code, mime=mime,
                        content_length=length, accept_ranges=ranges, sample=bytes(buf),
                    )
            except httpx.HTTPError as exc:
                raise SafeFetchError("NETWORK_ERROR", f"探针请求失败：{exc}") from exc
        raise SafeFetchError("SOURCE_BLOCKED", f"重定向超过 {MAX_REDIRECTS} 跳")


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


@dataclass
class StreamResult:
    """流式读取结果：最终地址、HTTP 状态与实际字节数；不持有响应体。"""
    url: str
    status_code: int
    bytes_read: int


def stream_to_sink(
    url: str,
    *,
    sink,
    max_bytes: int,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    should_cancel=None,
    on_progress=None,
) -> StreamResult:
    """有界流式 GET（docs/11 §5.2）：逐块写 sink，不在内存持有完整响应。

    - 每跳重定向重新校验 DNS/IP，跨主机跳转剥离敏感头（与 safe_fetch 同规则）。
    - 累计字节超过 max_bytes 抛 PAYLOAD_TOO_LARGE；连接/读取超时按超时抛出。
    - should_cancel() 返回真时抛 CANCELLED，立即关闭连接（调用方用于让出/取消）。
    - on_progress(bytes_read) 在每块写入后回调，用于续租与空闲检查；回调异常向上传播。
    """
    settings = get_settings()
    _check_url_allowed(url)

    transport = httpx.HTTPTransport(retries=0)
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout, connect=10.0, read=timeout),
        follow_redirects=False,
        headers={"User-Agent": "KnowledgeInbox/0.1 (+restricted-fetcher)"},
    ) as client:
        current = url
        current_host = (urlparse(current).hostname or "").lower()
        for _ in range(MAX_REDIRECTS + 1):
            try:
                hop_headers = {
                    k: v for k, v in (headers or {}).items()
                    if not (current_host != (urlparse(url).hostname or "").lower()
                            and k.lower() in _SENSITIVE_HEADERS)
                }
                # 流式读取：先拿响应头，确认非重定向后逐块消费 body
                with client.stream("GET", current, headers=hop_headers) as resp:
                    if resp.is_redirect:
                        next_url = _verify_redirect(current, resp)
                        if next_url is None:
                            raise SafeFetchError("SOURCE_BLOCKED", "重定向缺少 Location")
                        current = next_url
                        current_host = (urlparse(current).hostname or "").lower()
                        continue
                    status_code = resp.status_code
                    if status_code >= 400:
                        resp.read()
                        raise SafeFetchError(
                            "HTTP_ERROR", f"响应状态码 HTTP {status_code}", status_code=status_code
                        )
                    total = 0
                    for chunk in resp.iter_bytes(chunk_size=settings.stream_chunk_bytes):
                        if should_cancel is not None and should_cancel():
                            raise SafeFetchError("CANCELLED", "读取已被取消")
                        total += len(chunk)
                        if total > max_bytes:
                            raise SafeFetchError("PAYLOAD_TOO_LARGE", f"流超过 {max_bytes} 字节上限")
                        sink(chunk)
                        if on_progress is not None:
                            on_progress(total)
                    return StreamResult(url=str(resp.url), status_code=status_code, bytes_read=total)
            except httpx.HTTPError as exc:
                raise SafeFetchError("NETWORK_ERROR", f"流读取失败：{exc}") from exc
        raise SafeFetchError("SOURCE_BLOCKED", f"重定向超过 {MAX_REDIRECTS} 跳")

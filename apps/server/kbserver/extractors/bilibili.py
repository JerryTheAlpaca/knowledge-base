"""B 站字幕提取器（docs/04 专项设计）。

分层（docs/04 §3）：
    resolve_share_url → resolve_video_part → discover_tracks → choose_track
    → download_track → normalize_subtitles

实现依据（2026-09-08 复测，docs/06）：
- GET https://api.bilibili.com/x/web-interface/view?bvid=…  匿名可用，
  data.pages[] 携带 page/cid/duration/part；data.subtitle.list 匿名可见
  （轨道 id/lan/ai_type），但 subtitle_url 为空；轨字段名是 lan（不是 language）。
- GET https://api.bilibili.com/x/player/v2?bvid=…&cid=…  匿名可用，data 里
  携带明确的 need_login_subtitle=true 信号，data.subtitle.subtitles[] 为空列表；
  即字幕内容需登录态才能取得。WBI 路径（/x/player/wbi/v2）非必需：普通路径
  仍返回完整元数据与登录信号（2026-09-08 实测 code=0）。
- 因此：player 的 need_login_subtitle 是最可靠的登录判定来源；view 的字幕
  列表是视频级辅助信息，不能用于证明某个分 P 有或没有字幕。绝不声称有轨
  视频“无字幕”（A18）。

规则：
- 站点内部接口封装在本模块内，不是本项目的稳定 API（docs/02 §5.6）。
- 所有网络访问经 security.safe_fetch，不绕过 DNS/IP/重定向校验。
- 失败状态区分 available / login_required / no_track / blocked /
  network_error / unsupported / video_part_not_found / video_not_found /
  unconfirmed；unconfirmed 表示平台证据不足、无法确认有无字幕，
  请求失败、验证页或 JSON 解析失败不归类为 no_track（docs/04 §4.3）。
- 字幕下载地址过期允许重新探测一次，不循环重试（docs/04 §4.5）。
- 登录凭据（SESSDATA）只发往 api.bilibili.com 接口；字幕 CDN 下载默认
  不携带 Cookie（docs/05 §3.2）。
- 弹幕不是字幕；不下载音频；不启动 ASR；不伪造全文（docs/04 §1、§5）。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from ..security.safe_fetch import SafeFetchError, safe_fetch
from . import subtitles as subfmt

EXTRACTOR_VERSION = "bilibili_subtitles-1.1.0"

# 请求间的小间隔，避免对站点接口形成突发压力
_REQUEST_GAP_S = 0.6

_BILI_HOSTS = ("bilibili.com", "b23.tv")
# 允许携带登录凭据的主机：只有 B 站认证接口；字幕 CDN（*.hdslb.com）不携带
_CRED_ALLOWED_HOSTS = ("api.bilibili.com",)
_BV_PATTERN = re.compile(r"/video/(BV[0-9A-Za-z]+)")
_AV_PATTERN = re.compile(r"/video/av(\d+)", re.IGNORECASE)
_URL_IN_TEXT = re.compile(r"https?://[^\s，,、）)】\]]+")
# 风控/限流响应码（平台拒绝访问，不是“无字幕”）
_BLOCKED_CODES = {-412, -352, -799}
# 需要登录的响应码
_LOGIN_CODES = {-101, -111}


class BilibiliError(Exception):
    """提取失败。status 表示失败/发现状态，worker 据此决定重试或进入补充材料。"""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class VideoRef:
    """从分享链接解析出的视频标识。"""
    bvid: str | None = None
    aid: str | None = None
    explicit_part: int | None = None
    original_url: str = ""
    resolved_url: str = ""  # 短链展开后的最终 URL


@dataclass
class BilibiliExtraction:
    """成功提取结果：元数据 + 原始字幕 + 统一片段（发布前的全部材料）。"""
    video: VideoRef
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    duration_s: float | None
    pages_count: int
    part: int
    cid: str
    part_note: str | None
    tracks: list[dict] = field(default_factory=list)   # 可用轨元数据，不含临时下载地址
    track: dict | None = None                          # 所选轨
    login_state_used: bool = False
    raw_subtitle: bytes = b""
    raw_suffix: str = "json"
    segments: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> str:
        # 拿到并完整转换了所选字幕轨，即完成本次正文范围（docs/02 §5.3）
        return "full_text" if self.segments else "metadata_only"


def _browser_headers(url: str, sessdata: str | None = None) -> dict[str, str]:
    """请求头。SESSDATA 只发往明确允许的 B 站认证接口，绝不发给字幕 CDN。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }
    if sessdata:
        host = (urlparse(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in _CRED_ALLOWED_HOSTS):
            # 最小凭据：仅 SESSDATA（2026-09-07 实测优于整串 Cookie，见 docs/04 §5）
            headers["Cookie"] = f"SESSDATA={sessdata}"
    return headers


def _fetch_json(url: str, *, max_bytes: int, timeout: float = 15.0,
                sessdata: str | None = None) -> dict:
    try:
        result = safe_fetch(url, max_bytes=max_bytes, timeout=timeout,
                            headers=_browser_headers(url, sessdata))
    except SafeFetchError as exc:
        if exc.code == "NETWORK_ERROR":
            raise BilibiliError("network_error", f"接口请求失败：{exc}") from exc
        raise BilibiliError("blocked", f"接口请求被拒绝：{exc}") from exc
    # 先按 HTTP 状态分流，再解析 JSON（docs/05 §3.2：_fetch_json 明确分流）
    status = result.status_code
    if status in (401, 403):
        raise BilibiliError("login_required", f"接口要求登录（HTTP {status}）")
    if status in (412, 429):
        raise BilibiliError("blocked", f"平台限流或风控（HTTP {status}）")
    if status >= 500:
        raise BilibiliError("network_error", f"平台临时错误（HTTP {status}）")
    if status >= 400:
        raise BilibiliError("blocked", f"接口请求被拒绝（HTTP {status}）")
    try:
        doc = json.loads(result.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BilibiliError("blocked", "接口返回内容不是 JSON（可能被风控或需登录）") from exc
    if not isinstance(doc, dict):
        raise BilibiliError("blocked", "接口返回结构异常")
    return doc


def _api(doc: dict) -> dict:
    """校验 B 站接口 code 并返回 data；按响应码区分失败状态。"""
    code = doc.get("code")
    if code == 0:
        data = doc.get("data")
        if not isinstance(data, dict):
            raise BilibiliError("blocked", "接口 code=0 但缺少 data")
        return data
    if code in _BLOCKED_CODES:
        raise BilibiliError("blocked", f"平台拒绝访问（code={code}）")
    if code in _LOGIN_CODES:
        raise BilibiliError("login_required", f"该接口需要登录（code={code}）")
    if code == -404:
        raise BilibiliError("video_not_found", "视频不存在或已删除")
    raise BilibiliError("network_error", f"接口返回异常 code={code}：{doc.get('message')}")


# ---- 分层实现（docs/04 §3） ----

def extract_first_url(share_text: str | None) -> str | None:
    """从完整分享文字提取第一条 URL（保留原分享文字由调用方负责）。"""
    if not share_text:
        return None
    m = _URL_IN_TEXT.search(share_text)
    return m.group(0).rstrip(".,;！!?？") if m else None


def resolve_share_url(url: str) -> VideoRef:
    """展开 b23.tv 短链并解析 BV/aid 与显式分 P（docs/04 §4.1）。

    每一跳由 safe_fetch 做域名/DNS/IP 校验；短链按 GET 展开后丢弃页面内容。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in _BILI_HOSTS):
        raise BilibiliError("unsupported", f"不是 B 站链接：{host or url}")

    resolved = url
    if host.endswith("b23.tv"):
        result = safe_fetch(url, timeout=15.0, headers=_browser_headers(url))
        resolved = result.url
        parsed = urlparse(resolved)
        host = (parsed.hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in _BILI_HOSTS):
            raise BilibiliError("unsupported", f"短链跳转到非 B 站地址：{resolved}")

    ref = VideoRef(original_url=url, resolved_url=resolved)
    path = parsed.path or ""
    m = _BV_PATTERN.search(path)
    if m:
        ref.bvid = m.group(1)
    else:
        m = _AV_PATTERN.search(path)
        if m:
            ref.aid = m.group(1)
    if ref.bvid is None and ref.aid is None:
        qs = parse_qs(parsed.query or "")
        bv = (qs.get("bvid") or [None])[0]
        if bv:
            ref.bvid = bv
    if ref.bvid is None and ref.aid is None:
        raise BilibiliError("unsupported", f"链接中未找到 BV/av 视频标识：{resolved}")

    p_values = parse_qs(parsed.query or "").get("p")
    if p_values:
        try:
            ref.explicit_part = int(p_values[0])
        except ValueError:
            ref.explicit_part = None
    return ref


def resolve_video_part(ref: VideoRef, settings_max_bytes: int) -> dict:
    """查询视频元数据，确定当前分 P 与 cid（docs/04 §4.2）。

    显式 p 越界抛 video_part_not_found；未指定 p 且多 P 时按 P1 保存并注明，
    不声称知道用户正在播放哪一 P。
    """
    if ref.bvid:
        view_url = f"https://api.bilibili.com/x/web-interface/view?bvid={ref.bvid}"
    else:
        view_url = f"https://api.bilibili.com/x/web-interface/view?aid={ref.aid}"
    data = _api(_fetch_json(view_url, max_bytes=settings_max_bytes))

    pages = data.get("pages") or []
    if not pages:
        raise BilibiliError("video_not_found", "视频没有可用的分 P 信息")
    pages = sorted(pages, key=lambda p: int(p.get("page") or 1))

    explicit = ref.explicit_part
    part_note: str | None = None
    if explicit is not None:
        chosen = next((p for p in pages if int(p.get("page") or 0) == explicit), None)
        if chosen is None:
            raise BilibiliError(
                "video_part_not_found",
                f"分 P {explicit} 不存在（视频共 {len(pages)} 个分 P）",
            )
    else:
        chosen = pages[0]
        if len(pages) > 1:
            part_note = f"该链接未指定分 P，保存 P1（视频共 {len(pages)} 个分 P，可补充其他分 P）。"

    pubdate = data.get("pubdate")
    published_at = None
    if isinstance(pubdate, (int, float)):
        published_at = datetime.fromtimestamp(int(pubdate), tz=timezone.utc).isoformat()

    video_id = data.get("bvid") or ref.bvid or (f"av{ref.aid}" if ref.aid else "")
    page_num = int(chosen.get("page") or 1)

    # view API 的字幕轨清单匿名可见（subtitle_url 为空）：用于区分
    # “确实无轨”与“有轨但需要登录”（A18，2026-09-07 实测）
    view_tracks: list[dict] = []
    for t in (data.get("subtitle") or {}).get("list") or []:
        if not isinstance(t, dict):
            continue
        view_tracks.append({
            "track_id": str(t.get("id") or ""),
            "language": t.get("lan"),
            "label": t.get("lan_doc") or t.get("lan"),
            "is_auto_generated": _auto_flag(t),
            "is_translation": None,
            "kind": "caption",
        })

    return {
        "canonical_url": f"https://www.bilibili.com/video/{video_id}/" + (f"?p={page_num}" if page_num != 1 else ""),
        "title": data.get("title"),
        "author": (data.get("owner") or {}).get("name"),
        "published_at": published_at,
        "duration_s": float(data.get("duration") or 0) or None,
        "pages_count": len(pages),
        "page": page_num,
        "cid": str(chosen.get("cid")),
        "page_duration_s": float(chosen.get("duration") or 0) or None,
        "part_note": part_note,
        "view_subtitle_tracks": view_tracks,
    }


def _auto_flag(t: dict) -> bool | None:
    """自动字幕判定：lan 以 ai- 开头是平台明确标记。

    实测轨字段名是 lan（2026-09-08）；language 仅作历史兜底。
    ai_type 0/1 与 lan 信号一致时采用；两者都缺失保留 unknown（None）。
    """
    lan = (t.get("lan") or t.get("language") or "").lower()
    if lan.startswith("ai-"):
        return True
    ai_type = t.get("ai_type")
    if ai_type == 1:
        return True
    if ai_type == 0:
        return False
    return None


@dataclass
class TrackDiscovery:
    """player 接口的结构化探测结果（docs/05 §3.2：空列表不等于无字幕）。

    pairs       可下载轨 (track_meta, download_url)
    raw_count   player 返回的轨道总数（过滤弹幕后，含无地址轨）
    need_login  player 的 need_login_subtitle 明确登录信号
    video_level_view_tracks  view 接口的视频级字幕清单条数（辅助信息）
    """

    pairs: list[tuple[dict, str]] = field(default_factory=list)
    raw_count: int = 0
    need_login: bool = False
    video_level_view_tracks: int = 0


def discover_tracks(ref: VideoRef, page: dict, settings_max_bytes: int,
                    sessdata: str | None = None) -> TrackDiscovery:
    """探测字幕轨，转换为内部契约（docs/04 §4.3）。

    返回结构化探测结果：保留登录提示、原始轨数与可下载轨数等非秘密元数据，
    避免空列表被等同于“无字幕”。带登录态时仅对 api.bilibili.com 附最小凭据
    Cookie（SESSDATA），不携带其他 Cookie 字段。
    """
    if ref.bvid:
        player_url = f"https://api.bilibili.com/x/player/v2?bvid={ref.bvid}&cid={page['cid']}"
    else:
        player_url = f"https://api.bilibili.com/x/player/v2?aid={ref.aid}&cid={page['cid']}"
    try:
        data = _api(_fetch_json(player_url, max_bytes=settings_max_bytes, sessdata=sessdata))
    except BilibiliError as exc:
        if sessdata and exc.status == "login_required":
            raise BilibiliError(
                "login_required", "B 站登录凭据已失效或无权限：请在设置中更新 B 站登录态。"
            ) from exc
        raise

    subtitle_section = data.get("subtitle") or {}
    raw_tracks = subtitle_section.get("subtitles") or []
    pairs: list[tuple[dict, str]] = []
    raw_count = 0
    for t in raw_tracks:
        if not isinstance(t, dict):
            continue
        if t.get("kind") == "danmaku":  # 弹幕不是字幕（防御性过滤，docs/04 §4.4）
            continue
        raw_count += 1
        url = t.get("subtitle_url") or ""
        if not url:
            continue  # 无地址的轨（如翻译投稿轨）不参与选择
        if url.startswith("//"):
            url = "https:" + url
        track = {
            "track_id": str(t.get("id") or t.get("lan") or len(pairs) + 1),
            "language": t.get("lan"),
            "label": t.get("lan_doc") or t.get("lan"),
            "is_auto_generated": _auto_flag(t),
            "is_translation": None,
            "kind": "caption",
        }
        pairs.append((track, url))
    return TrackDiscovery(
        pairs=pairs,
        raw_count=raw_count,
        need_login=bool(data.get("need_login_subtitle")),
        video_level_view_tracks=len(page.get("view_subtitle_tracks") or []),
    )


def choose_track(pairs: list[tuple[dict, str]]) -> tuple[dict, str]:
    """按语言偏好选轨：优先中文；同语言优先人工轨（docs/04 §4.4）。"""
    if not pairs:
        raise BilibiliError("no_track", "没有可选字幕轨")

    def is_zh(t: dict) -> bool:
        lang = (t.get("language") or "").lower()
        return lang.startswith("zh") or lang.startswith("ai-zh")

    def sort_key(pair: tuple[dict, str]):
        t = pair[0]
        auto = t.get("is_auto_generated")
        manual_rank = 0 if auto is False else (1 if auto is None else 2)
        return (0 if is_zh(t) else 1, manual_rank)

    return sorted(pairs, key=sort_key)[0]


class _SubtitleBodyInvalid(Exception):
    """下载内容不是字幕 JSON（地址过期/需登录/风控页）。"""

    def __init__(self, raw: bytes):
        super().__init__("字幕内容不是可解析的字幕 JSON")
        self.raw = raw


def _download_and_parse(url: str, settings_max_bytes: int) -> tuple[bytes, list[dict]]:
    """下载并解析字幕 JSON；登录页/风控页不当作字幕保存（docs/04 §4.5）。

    字幕 CDN（*.hdslb.com）下载不携带 SESSDATA（docs/05 §3.2）；
    SafeFetchError 按具体错误码分类，网络故障与被拒不混为一谈。
    """
    try:
        result = safe_fetch(url, max_bytes=settings_max_bytes, timeout=15.0,
                            headers=_browser_headers(url))
        raw = result.content
        status = result.status_code
    except SafeFetchError as exc:
        if exc.code == "NETWORK_ERROR":
            raise BilibiliError("network_error", f"字幕下载失败：{exc}") from exc
        raise BilibiliError("blocked", f"字幕下载被拒绝：{exc}") from exc
    if status in (401, 403):
        raise BilibiliError("blocked", f"字幕下载被拒绝（HTTP {status}；地址可能已过期）")
    if status >= 400:
        raise BilibiliError("blocked", f"字幕下载失败（HTTP {status}）")
    try:
        records = subfmt.parse_platform_json(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _SubtitleBodyInvalid(raw) from exc
    return raw, records


# ---- 总入口 ----

def extract(url: str, *, share_text: str | None = None,
            sessdata: str | None = None) -> BilibiliExtraction:
    """提取一条用户指定视频/分 P 的字幕；失败抛 BilibiliError。

    sessdata 为该用户托管的 B 站登录态最小凭据；提供时以登录态探测 player
    接口并下载字幕。只处理用户选择的这一个视频/分 P，不抓合集、播放列表
    或作者主页。
    """
    from ..config import get_settings

    settings = get_settings()
    limit = settings.subtitle_download_limit

    target = url or extract_first_url(share_text)
    if not target:
        raise BilibiliError("unsupported", "分享内容中没有可处理的 B 站链接")

    ref = resolve_share_url(target)
    page = resolve_video_part(ref, limit)
    time.sleep(_REQUEST_GAP_S)
    discovery = discover_tracks(ref, page, limit, sessdata=sessdata)
    pairs = discovery.pairs

    if not pairs:
        # player 无可下载轨：先看 player 的明确登录信号（2026-09-08 实测：
        # 匿名 player 返回 need_login_subtitle=true），再参考 view 视频级清单。
        if discovery.need_login:
            if sessdata:
                raise BilibiliError(
                    "login_required",
                    "B 站明确提示需要登录，但当前登录态未取得字幕（凭据可能已失效）。"
                    "请在设置中更新 B 站登录态，或补充字幕文件/粘贴摘录。",
                )
            raise BilibiliError(
                "login_required",
                "B 站提示该视频字幕需要登录才能取得。可在设置中托管 B 站登录态自动获取，"
                "或补充字幕文件/粘贴摘录。",
            )
        if discovery.raw_count > 0:
            # player 列出了轨道但没有可下载地址、也没有登录信号：平台返回不完整，
            # 如实标「未能取得」，不声称字幕不存在（docs/05 §3.2）
            raise BilibiliError(
                "unconfirmed",
                f"播放器列出 {discovery.raw_count} 条字幕轨但未提供可下载地址"
                "（可能需要登录或仅有翻译轨）。可在设置中托管 B 站登录态后重试，"
                "或补充字幕文件/粘贴摘录。",
            )
        view_tracks = page.get("view_subtitle_tracks") or []
        if view_tracks and page.get("pages_count", 1) == 1:
            # 单 P 视频：view 视频级清单就是本视频的字幕轨，player 却没给可下载地址
            langs = "、".join(
                dict.fromkeys(
                    (t.get("label") or t.get("language") or "?") for t in view_tracks[:6]
                )
            )
            detail = f"视频存在 {len(view_tracks)} 条字幕轨（{langs}{'…' if len(view_tracks) > 6 else ''}）"
            if sessdata:
                raise BilibiliError(
                    "login_required",
                    f"{detail}，但当前登录态未取得可下载字幕"
                    "（可能只有翻译投稿轨或凭据已失效）。请核对 B 站登录态或补充字幕文件。",
                )
            raise BilibiliError(
                "login_required",
                f"{detail}，但匿名访问无法取得字幕内容。"
                "可在设置中托管 B 站登录态自动获取，或补充字幕文件/粘贴摘录。",
            )
        if view_tracks:
            # 多 P 视频：view 清单是视频级信息，不能证明本分 P 有/无字幕
            #（docs/05 §3.2：以所选 cid 的播放器响应为准，证据不足不妄下结论）
            raise BilibiliError(
                "unconfirmed",
                f"P{page.get('page')} 播放器未列出可下载字幕；视频级字幕清单有 "
                f"{len(view_tracks)} 条轨道但无法确认本分 P 是否有字幕。"
                "可补充字幕文件/粘贴摘录，或在浏览器确认后重试。",
            )
        # player 与 view 皆无轨道：可能确实无字幕，也可能是仅自动字幕。如实提示（docs/04 §5）。
        if sessdata:
            raise BilibiliError(
                "no_track",
                "登录态下仍未取得字幕轨：视频可能没有独立字幕。"
                "若你在浏览器中能看到可开关字幕，请补充字幕文件或粘贴摘录。",
            )
        raise BilibiliError(
            "no_track",
            "匿名访问未取得字幕轨：视频可能没有独立字幕，或自动字幕需要登录。"
            "可在设置中托管 B 站登录态自动获取，或补充字幕文件/粘贴摘录。",
        )

    track, track_url = choose_track(pairs)
    time.sleep(_REQUEST_GAP_S)
    try:
        raw, records = _download_and_parse(track_url, limit)
    except _SubtitleBodyInvalid:
        # 地址过期或返回异常：重新探测一次同一视频/分 P（docs/04 §4.5），不循环重试
        time.sleep(_REQUEST_GAP_S)
        discovery2 = discover_tracks(ref, page, limit, sessdata=sessdata)
        retry_pair = next((p for p in discovery2.pairs if p[0]["track_id"] == track["track_id"]), None)
        if retry_pair is None:
            # 轨道临时消失不能标“视频无字幕”（docs/05 §3.2）
            raise BilibiliError(
                "unconfirmed", "字幕地址过期且重新探测未找到所选轨道；请稍后重试或补充字幕文件。"
            )
        try:
            raw, records = _download_and_parse(retry_pair[1], limit)
        except (_SubtitleBodyInvalid, BilibiliError) as exc:
            raise BilibiliError(
                "blocked", "字幕地址重新探测后仍无法取得有效字幕 JSON；原始材料不受影响。"
            ) from exc

    video_duration = page.get("page_duration_s") or page.get("duration_s")
    segments, warnings = subfmt.normalize_records(
        records, source="platform_subtitle", video_duration_s=video_duration
    )
    if page.get("part_note"):
        warnings.append(page["part_note"])

    return BilibiliExtraction(
        video=ref,
        canonical_url=page["canonical_url"],
        title=page["title"],
        author=page["author"],
        published_at=page["published_at"],
        duration_s=page.get("duration_s"),
        pages_count=page["pages_count"],
        part=page["page"],
        cid=page["cid"],
        part_note=page.get("part_note"),
        tracks=[t for t, _u in pairs],
        track=track,
        login_state_used=bool(sessdata),
        raw_subtitle=raw,
        raw_suffix="json",
        segments=segments,
        warnings=warnings,
    )

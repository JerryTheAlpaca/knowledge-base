"""小红书适配器（docs/18 §7.3）。

处理对象：公开笔记页（图文/视频）。短链 xhslink.com / xhslink.cn
（P0 实测 2026-09-17 新增形态）先展开再解析笔记 ID；解析不出稳定
ID 时用确定性 slug 归档并在缺失清单如实说明。

解析优先 window.__INITIAL_STATE__ 稳定结构（noteMap），退回已知的
正文容器；只提取笔记标题/正文/标签与作者，不把推荐流、评论或页面
导航混入正文。图文笔记按页面顺序保存可访问原图（限量，缺失如实
记录）；视频笔记保存文字说明与封面引用，不拉取原视频。

P0 实测（2026-09-17）：
- /explore/{id} 或 /discovery/item/{id} 匿名 → 302 → /404/sec_*（
  error_code=300031「当前笔记暂时无法浏览」）；
- 带 xsec_token 的分享链接匿名 → 302 → /login?redirectPath=<编码的
  笔记地址>；redirectPath 里携带笔记 ID 与 xsec_token（分享授权
  凭证，含尾随 =）。
自动读取按「先匿名 → 登录限制时用用户托管会话对真实笔记地址重试
一次 → 仍失败如实进入补充材料」实现；图片/封面 CDN 默认不带登录
态（docs/18 §7.2 规则 4）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse, urlunparse

from ..config import get_settings
from ..domain.platform_sessions import SPECS
from .fetch_base import (
    PlatformError,
    cookie_send_allowed,
    download_images,
    extract_first_url,
    fetch_page,
    page_slug,
    status_error,
)

EXTRACTOR_VERSION = "xiaohongshu_note-1.1.2"

_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item|user/profile)/([0-9a-f]{16,32})(?:[/?#]|$)")
_USER_NOTE_RE = re.compile(r"/user/profile/([0-9a-f]{16,32})/([0-9a-f]{16,32})")
_SEC_MARK = "/404/sec_"
_LOGIN_MARKS = ("当前笔记暂时无法浏览", "请完成登录后继续", "登录后即可查看")
# 防盗链：图片请求带页面 Referer（不带登录态）
_IMG_HOST_MARK = "xiaohongshu.com"

# 登录态可发往、且笔记页可能落在的精确一方域名（docs/18 §7.2 规则 4）
_COOKIE_DOMAINS = SPECS["xiaohongshu"].cookie_send_domains

# 只定位赋值起点，对象边界交给 _balanced_span_end 配平：尾随 `;` 或后续脚本
# 都会让「右括号紧跟 </script>」式正则整体失配（审查 C-16）
_STATE_ASSIGN_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*\{")


def parse_note_id(url: str | None) -> str | None:
    """从笔记页 URL 提取稳定笔记 ID；解析不出返回 None。"""
    if not url:
        return None
    path = urlparse(url).path or ""
    m = _USER_NOTE_RE.search(path)
    if m:
        return m.group(2)
    m = _NOTE_ID_RE.search(path)
    if m:
        return m.group(1)
    return None


@dataclass
class XiaohongshuExtraction:
    """成功提取结果（与 webpages.WebpageExtraction 同构的最小字段）。"""
    note_id: str
    media_kind: str  # text（图文）| video
    content_scope: str  # image_post | video_post
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    raw_html: bytes
    raw_mime: str
    segments: list[dict] = field(default_factory=list)
    images: list = field(default_factory=list)  # fetch_base.ImageDownload
    missing_materials: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    login_state_used: bool = False
    # 正文来源：note（结构化字段）| detail_desc（正文容器）| og_description | title_only
    body_source: str = "note"

    @property
    def coverage(self) -> str:
        # 只有命中笔记正文（结构化字段或正文容器）才算全文：标题、og 摘要
        # 冒充 full_text 会让 AI 拿标题加工、Obsidian 显示「已取得全文」（审查 C-08）
        if self.body_source in ("note", "detail_desc"):
            return "full_text" if self.segments else "metadata_only"
        return "partial_text" if self.body_source == "og_description" else "metadata_only"

    @property
    def extractor_name(self) -> str:
        return "xiaohongshu_note"

    @property
    def original_path(self) -> str:
        return f"originals/xiaohongshu/{self.note_id}/page.html"

    @property
    def source_locator(self) -> dict:
        return {"type": "xiaohongshu_note", "content_id": self.note_id,
                "final_url": self.canonical_url,
                "content_scope": self.content_scope}


def extract(url: str | None, *, share_text: str | None = None,
            cookies: dict[str, str] | None = None,
            include_images: bool = False) -> XiaohongshuExtraction:
    """提取一篇公开笔记；失败抛 PlatformError（docs/18 §7.3 失败语义）。

    include_images 控制原图下载（解析出的图片顺序始终记录）；视频笔记
    只保存文字说明与封面存在性，不默认拉取原视频。
    """
    settings = get_settings()
    target = (url or "").strip() or extract_first_url(share_text)
    if not target:
        raise PlatformError("unsupported_type", "没有可处理的小红书链接")
    host = (urlparse(target).hostname or "").lower()
    if "xiaohongshu.com" not in host and not (
        host.endswith("xhslink.com") or host.endswith("xhslink.cn")
    ):
        raise PlatformError("unsupported_type", f"不是小红书链接：{target[:200]}")

    # 首跳（短链在此展开）；展开落在登录页时从 redirectPath 解出真实
    # 笔记地址（含 xsec_token），后续解析与会话重试都用它
    res = fetch_page(target, max_bytes=settings.html_download_limit)
    login_state_used = False
    note_url = _note_url_from_login(res.url) or res.url
    # 带登录态前确认落点归属：redirectPath 与重定向落点都是外部可控值，
    # 不能因为「看起来是笔记链接」就把用户 Cookie 发往任意主机（审查 C-02）
    if _needs_login(res) and cookies and cookie_send_allowed(note_url, _COOKIE_DOMAINS):
        res = fetch_page(note_url, cookies=cookies, cookie_domains=_COOKIE_DOMAINS,
                         max_bytes=settings.html_download_limit)
        login_state_used = True
        note_url = _note_url_from_login(res.url) or res.url
    if _needs_login(res):
        raise PlatformError(
            "login_required",
            "小红书笔记需要登录才能查看，自动读取暂不可用；链接已保存。"
            "请粘贴笔记正文，或上传笔记截图。",
        )
    if res.status_code >= 400:
        raise status_error(
            res.status_code,
            deleted="笔记已删除或不可见；如仍存在，可粘贴正文或补充截图。",
            blocked="小红书暂时拒绝了这次读取（风控或限流）；链接已保存，可稍后点「重新提取」，"
                    "也可以先粘贴正文或补充截图。",
        )

    body = res.content.decode("utf-8", errors="replace")
    note_id = parse_note_id(note_url) or parse_note_id(target) or ""

    state = _load_initial_state(body)
    note = _dig_note(state, note_id)
    title = author = published_at = None
    desc = ""
    body_source = "title_only"
    image_urls: list[str] = []
    media_kind, content_scope = "text", "image_post"

    if note is not None:
        title = _s(note, "title")
        desc = _s(note, "desc") or ""
        author = _author_of(state, note)
        published_at = _ts_of(note)
        note_card = note.get("noteCard") if isinstance(note.get("noteCard"), dict) else {}
        note_type = _s(note_card, "type") or _s(note, "type") or ""
        if "video" in note_type.lower():
            media_kind, content_scope = "video", "video_post"
            cover = note_card.get("cover") if isinstance(note_card.get("cover"), dict) else {}
            cover_url = _s(cover, "url") or _s(cover, "url_default")
            if cover_url:
                image_urls.append(cover_url)
        else:
            img_list = note_card.get("imageList") or note.get("imageList")
            if isinstance(img_list, list):
                for item in img_list:
                    if not isinstance(item, dict):
                        continue
                    u = _s(item, "url") or _s(item, "url_default")
                    if u and u not in image_urls:
                        image_urls.append(u)
        if desc:
            body_source = "note"

    # 结构未命中或没有正文时先跑容器兜底，都不命中才终态：旧实现只要 body
    # 含 noteDetailMap 就抛 structure_changed，页面小抖动即终态且兜底永不执行
    # （审查 C-16）
    if not desc:
        desc, title, author, fell_back = _container_fallback(body, title, author)
        if desc:
            body_source = fell_back
    if not desc and not title:
        if state is not None or "noteDetailMap" in body or "noteList" in body:
            raise PlatformError(
                "structure_changed",
                "小红书页面可访问但已知结构与正文容器均未命中，无法提取笔记内容；请粘贴正文或补充截图。",
            )
        raise PlatformError(
            "empty_content",
            "未能从笔记页提取到内容：页面可能由脚本渲染或需要登录。"
            "可粘贴正文保存，或补充截图。",
        )

    text = "\n".join(part for part in (title or "", desc) if part)
    segments = [
        {
            "segment_id": f"s{i:04d}",
            "text": line,
            "artifact_file_id": None,
            "locator": {"type": "xiaohongshu_note", "content_id": note_id or None,
                        "index": i},
            "origin": "platform_content",
            "confidence": None,
            "kind": "paragraph",
        }
        for i, line in enumerate(
            [ln.strip() for ln in text.split("\n") if ln.strip()], start=1
        )
    ]

    images: list = []
    missing: list[str] = []
    warnings: list[str] = []
    if media_kind == "video":
        missing.append("原视频未保存：视频笔记只保存文字说明与封面，不下载视频内容。")
    if image_urls and include_images:
        # 图文=正文原图（限量）；视频=仅封面
        images, img_missing = download_images(
            image_urls, referer="https://www.xiaohongshu.com/",
            max_bytes_per_image=settings.max_image_bytes,
            max_total_bytes=settings.images_total_bytes,
        )
        missing.extend(img_missing)
        if len(image_urls) > len(images):
            warnings.append(f"已取得 {len(image_urls)} 张图片引用中的 {len(images)} 张；未取得的已列入缺失清单。")
    elif image_urls:
        warnings.append(f"笔记含 {len(image_urls)} 张图片引用，本次未下载（默认不提取图片，需要时可在条目管理点「提取图片」重新提取）。")
    warnings.append("已留存原始 HTML 响应；页面脚本、样式与动态内容未归档，不构成完整镜像。")
    if body_source == "title_only":
        missing.insert(0, "笔记正文未取得：当前文字只是标题，页面正文容器与已知结构均未命中。")
    if not note_id:
        missing.insert(0, "未能从链接解析出稳定笔记 ID，归档路径使用确定性散列标识。")

    return XiaohongshuExtraction(
        note_id=note_id or page_slug(note_url),
        media_kind=media_kind,
        content_scope=content_scope,
        canonical_url=note_url,
        title=title[:300] if title else None,
        author=author[:120] if author else None,
        published_at=published_at,
        raw_html=res.content,
        raw_mime=res.mime or "text/html",
        segments=segments,
        images=images,
        missing_materials=missing,
        warnings=warnings,
        login_state_used=login_state_used,
        body_source=body_source,
    )


def _is_login_page(url: str) -> bool:
    """最终落点是小红书登录页（P0 实测：带 token 分享链接匿名 302 到这里）。"""
    path = (urlparse(url).path or "").rstrip("/")
    return path == "/login" or path.endswith("/login")


def _note_url_from_login(url: str) -> str | None:
    """登录页 URL 的 redirectPath 参数携带真实笔记地址（已含一层编码，
    parse_qs 解码后 xsec_token 的尾随 = 完整保留）。

    只接受本平台一方域名并统一升级到 https：这个地址随后可能带着用户托管
    Cookie 去请求，既不能指向任意主机，也不能明文传输（审查 C-02）。
    """
    if not _is_login_page(url):
        return None
    raw = (parse_qs(urlparse(url).query).get("redirectPath") or [None])[0]
    if not raw or not raw.startswith("http"):
        return None
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _COOKIE_DOMAINS:
        return None
    if parsed.scheme != "https":
        return urlunparse(parsed._replace(scheme="https"))
    return raw


def _needs_login(res: object) -> bool:
    if res.status_code in (401, 403):  # type: ignore[attr-defined]
        return True
    if _is_login_page(res.url):  # type: ignore[attr-defined]
        return True
    body = res.content.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
    if _SEC_MARK in body and "error_code=300031" in body:
        return True
    return any(mark in body for mark in _LOGIN_MARKS)


def _load_initial_state(body: str) -> dict | None:
    m = _STATE_ASSIGN_RE.search(body)
    if not m:
        return None
    # 从赋值起点按括号配平取对象：不再要求右括号紧跟 </script>（审查 C-16）
    raw = body[m.end() - 1:_balanced_span_end(body, m.end())]
    # __INITIAL_STATE__ 常含 undefined 字面量，JSON 不认
    raw = raw.replace("undefined", "null")
    # 2026-09-17 生产实测：字段值还可能是 JS 构造调用（noteDetailMap 等
    # 以 new Map([...]) 输出），不转换则整个 state 解析失败、笔记提取不到
    raw = _replace_js_literals(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _balanced_span_end(raw: str, start: int) -> int:
    """start 是开括号后的第一个字符；返回配对闭括号的下一位（字符串感知）。"""
    depth, i, n = 1, start, len(raw)
    in_str = esc = False
    while i < n and depth:
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
        i += 1
    return i


def _replace_js_literals(raw: str) -> str:
    """把 new Map([[k, v], ...]) / new Set([...]) 字面量转成 JSON 等价形式。

    Map → 对象（键一律 JSON 字符串化），Set → 数组；括号内不是纯 JSON
    数组时该段替换为 null，不影响其余字段解析。
    """
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        j = raw.find("new ", i)
        if j < 0:
            out.append(raw[i:])
            break
        is_map = raw.startswith("new Map(", j)
        is_set = raw.startswith("new Set(", j)
        if not (is_map or is_set):
            out.append(raw[i:j + 4])
            i = j + 4
            continue
        out.append(raw[i:j])
        end = _balanced_span_end(raw, j + 8)
        try:
            arr = json.loads(raw[j + 8:end - 1])
            items: list[str] = []
            if is_map and isinstance(arr, list):
                for pair in arr:
                    if isinstance(pair, list) and pair:
                        items.append(
                            f"{json.dumps(pair[0], ensure_ascii=False)}:"
                            f"{json.dumps(pair[1], ensure_ascii=False)}")
                out.append("{" + ",".join(items) + "}")
            else:
                out.append(json.dumps(arr, ensure_ascii=False))
        except json.JSONDecodeError:
            out.append("null")
        i = end
    return "".join(out)


def _dig_note(state: dict | None, note_id: str) -> dict | None:
    """initialState.note.noteDetailMap.{id}.note / noteMap.{id}，找不到返回 None。"""
    if not isinstance(state, dict):
        return None
    note_root = state.get("note")
    if not isinstance(note_root, dict):
        note_root = state
    for key in ("noteDetailMap", "noteMap", "firstNoteId"):
        bucket = note_root.get(key)
        if isinstance(bucket, dict) and note_id and note_id in bucket:
            node = bucket[note_id]
            if isinstance(node, dict):
                note = node.get("note") if isinstance(node.get("note"), dict) else node
                if isinstance(note, dict):
                    return note
    return None


def _s(node: dict, *keys: str) -> str | None:
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _author_of(state: dict | None, note: dict) -> str | None:
    user = note.get("user")
    if isinstance(user, dict):
        return _s(user, "nickname", "name", "nickName")
    return None


def _ts_of(note: dict) -> str | None:
    ts = note.get("time") or note.get("publishTime") or note.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        from datetime import datetime, timezone

        try:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            return dt.isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return _s(note, "time", "publishTime")


def _container_fallback(body: str, title: str | None, author: str | None):
    """无 __INITIAL_STATE__ 时读 og 元数据与已知正文容器。

    返回 (正文, 标题, 作者, 正文来源)；来源用于 coverage 判定，标题与 og
    摘要不算全文（审查 C-08）。
    """
    og_title = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]{1,300})"', body)
    og_desc = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]{1,2000})"', body)
    m = re.search(r'<div[^>]+id="detail-desc"[^>]*>(.*?)</div>', body, re.S)
    desc = ""
    source = "title_only"
    if m:
        desc = re.sub(r"<[^>]+>", " ", m.group(1))
        desc = re.sub(r"\s+", " ", desc).strip()
        if desc:
            source = "detail_desc"
    if not desc and og_desc:
        desc = og_desc.group(1).strip()
        source = "og_description" if desc else "title_only"
    if title is None and og_title:
        title = og_title.group(1).strip()
    return desc, title, author, source

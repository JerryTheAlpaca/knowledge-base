"""知乎适配器（docs/18 §7.5）。

处理对象：
- zhihu_answer：question/{qid}/answer/{aid}，只提取该回答；
- zhihu_article：zhuanlan.zhihu.com/p/{id}（或 /p/{id}），只提取该文章；
- 仅 question/{qid}：保存问题标题与链接，进入「请补充具体回答或正文」。

解析优先 js-initialData 稳定结构，退回正文容器；过滤相关推荐、其他
回答、评论与页脚。盐选/付费内容只保存公开可见部分（coverage=
partial_text），不猜测付费正文。

P0 实测（2026-09-16）：匿名请求 403 + zh-zse-ck JS 挑战壳；完整浏览器
Cookie（含 __zse_ck）仍 403——该签名与浏览器环境绑定，仅复制 Cookie
不能稳定读取。因此自动读取按「尝试 → 如实降级」实现：失败统一进入
补充材料（needs_input），不把挑战页、推荐流或标题冒充正文。原始响应
留存到 originals/zhihu/{content_type}/{id}/。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlparse

from ..config import get_settings
from ..domain.platform_sessions import SPECS
from .fetch_base import (
    PlatformError,
    cookie_send_allowed,
    download_images,
    extract_first_url,
    fetch_page,
    status_error,
)

EXTRACTOR_VERSION = "zhihu_page-1.1.0"

_ANSWER_RE = re.compile(r"/question/(\d{1,12})/answer/(\d{1,14})")
_QUESTION_RE = re.compile(r"/question/(\d{1,12})(?:/|$)")
_ARTICLE_RE = re.compile(r"/p/(\d{1,14})(?:/|$)")
# JS 挑战壳：650 字节左右、只含 zh-zse-ck meta（P0 实测特征）
_CHALLENGE_MARK = "zh-zse-ck"
# 知乎登录/风控落点路径（结构化判定；正文里出现「请登录」不等于登录墙，审查 C-15）
_LOGIN_PATHS = ("/account/login", "/account/unhuman", "/signin", "/login")

# 登录态只发往的精确一方域名（docs/18 §7.2 规则 4，审查 C-02）
_COOKIE_DOMAINS = SPECS["zhihu"].cookie_send_domains

_INITIAL_DATA_RE = re.compile(
    r'<script[^>]+id="js-initialData"[^>]*>(.*?)</script>', re.S
)


def parse_target(url: str | None) -> tuple[str, str, str | None, str | None] | None:
    """解析知乎 URL 为 (content_type, content_id, question_id, answer_id)。

    content_type ∈ zhihu_answer / zhihu_article / zhihu_question。
    无法识别返回 None（unsupported_type）。
    """
    if not url:
        return None
    path = urlparse(url).path or ""
    m = _ANSWER_RE.search(path)
    if m:
        return ("zhihu_answer", m.group(2), m.group(1), m.group(2))
    host = (urlparse(url).hostname or "").lower()
    m = _ARTICLE_RE.search(path)
    if m and (host == "zhuanlan.zhihu.com" or host.endswith(".zhihu.com")):
        return ("zhihu_article", m.group(1), None, m.group(1))
    m = _QUESTION_RE.search(path)
    if m:
        return ("zhihu_question", m.group(1), m.group(1), None)
    return None


@dataclass
class ZhihuExtraction:
    """成功提取结果（与 webpages.WebpageExtraction 同构的最小字段）。"""
    content_type: str  # zhihu_answer | zhihu_article | zhihu_question
    content_id: str
    question_id: str | None
    answer_id: str | None
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    raw_html: bytes
    raw_mime: str
    segments: list[dict] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    images: list = field(default_factory=list)  # fetch_base.ImageDownload
    missing_materials: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    login_state_used: bool = False
    question_only: bool = False
    partial_body: bool = False  # 盐选/付费：只有公开可见部分

    @property
    def coverage(self) -> str:
        # 与 webpages/xiaohongshu 同构的共同协议：images/missing_materials 必须存在，
        # worker 归档路径无条件读它们（审查 C-03）
        if self.question_only or not self.segments:
            return "metadata_only"
        return "partial_text" if self.partial_body else "full_text"

    @property
    def media_kind(self) -> str:
        return "text"

    @property
    def extractor_name(self) -> str:
        return "zhihu_page"

    @property
    def original_path(self) -> str:
        return f"originals/zhihu/{self.content_type}/{self.content_id}/page.html"

    @property
    def source_locator(self) -> dict:
        locator: dict = {"type": self.content_type, "content_id": self.content_id}
        if self.question_id:
            locator["question_id"] = self.question_id
        if self.answer_id and self.content_type == "zhihu_answer":
            locator["answer_id"] = self.answer_id
        if self.content_type == "zhihu_article":
            locator["article_id"] = self.answer_id
        return locator


# ---- 内容 HTML → 段落（保留引用/代码块/图片顺序） ----

_BLOCK_TAGS = {"p", "div", "section", "li", "dd", "dt", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _ContentParser(HTMLParser):
    """知乎正文 HTML → (blocks, image_urls)。

    blocks 是 (kind, text) 列表，kind ∈ paragraph/heading/quote/code。
    过滤节点：script/style/按钮类；图片只记 URL，不内联。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.image_urls: list[str] = []
        self._buf: list[str] = []
        self._kind = "paragraph"
        self._skip_depth = 0
        self._figure_depth = 0  # figure 内只收 img，不把说明文字外的排版噪声并入正文

    def _flush(self, kind: str | None = None):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf.clear()
        if text:
            self.blocks.append((kind or self._kind, text))

    def handle_starttag(self, tag, attrs):
        adict = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        if tag == "figure":
            self._figure_depth += 1
        if tag in _HEADING_TAGS:
            self._flush()
            self._kind = "heading"
        elif tag == "blockquote":
            self._flush()
            self._kind = "quote"
        elif tag == "pre":
            self._flush()
            self._kind = "code"
        elif tag in _BLOCK_TAGS:
            self._flush()
            self._kind = "paragraph"
        elif tag == "br":
            # br 是行内断行，保持同段
            self._buf.append(" ")
        if tag == "img":
            src = adict.get("data-actualsrc") or adict.get("data-original") or adict.get("src")
            if src and src.startswith(("http://", "https://")) and src not in self.image_urls:
                self.image_urls.append(src)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip_depth:
            self._skip_depth -= 1
        if tag == "figure" and self._figure_depth:
            self._figure_depth -= 1
        if tag in _HEADING_TAGS | {"blockquote", "pre"} | _BLOCK_TAGS:
            self._flush()
            self._kind = "paragraph"

    def handle_data(self, data):
        if self._skip_depth:
            return
        # figure 内的 figcaption 文字保留，其余排版噪声（如懒加载占位）只有文本就保留
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _parse_content_html(html: str) -> tuple[list[tuple[str, str]], list[str]]:
    parser = _ContentParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # 容错：畸形片段按已解析部分处理
    return parser.blocks, parser.image_urls


# ---- js-initialData 深挖 ----

def _dig_entity(data: dict, group: str, entity_id: str) -> dict | None:
    """initialState.entities.{group}.{entity_id}，找不到返回 None。"""
    state = data.get("initialState") if isinstance(data, dict) else None
    entities = state.get("entities") if isinstance(state, dict) else None
    bucket = entities.get(group) if isinstance(entities, dict) else None
    ent = bucket.get(entity_id) if isinstance(bucket, dict) else None
    return ent if isinstance(ent, dict) else None


def _ent_str(ent: dict, *keys: str) -> str | None:
    for k in keys:
        v = ent.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _ent_author(ent: dict) -> str | None:
    author = ent.get("author")
    if isinstance(author, dict):
        return _ent_str(author, "name", "headline")
    return None


def _ent_paid(ent: dict) -> bool:
    """盐选/付费标记：只认明确字段，不用文字猜测。"""
    return bool(ent.get("paid") or ent.get("isPaid") or ent.get("paidInfo"))


# ---- 总入口 ----

def _is_challenge(status: int, body: str) -> bool:
    return status == 403 and _CHALLENGE_MARK in body and len(body) < 2000


def extract(url: str | None, *, share_text: str | None = None,
            cookies: dict[str, str] | None = None,
            include_images: bool = False) -> ZhihuExtraction:
    """提取一个知乎回答/文章/问题页；失败抛 PlatformError。

    先匿名读取；明确遇到登录限制（挑战壳/登录墙）且用户托管了会话时
    带会话重试一次；仍失败如实进入补充材料（docs/18 §7.5）。
    include_images 只控制图片下载，解析出的图片 URL 始终记录。
    """
    settings = get_settings()
    target = (url or "").strip() or extract_first_url(share_text)
    parsed = parse_target(target)
    if parsed is None:
        raise PlatformError("unsupported_type", "不是可识别的知乎内容链接（支持回答、专栏文章与问题页）")
    content_type, content_id, question_id, answer_id = parsed

    # 首跳匿名；登录限制且有会话 → 带会话重试一次。Cookie 挂在本次调用的
    # 首跳上，所以先确认 target 的落点归属（审查 C-02）
    res = fetch_page(target, max_bytes=settings.html_download_limit)
    login_state_used = False
    if (_needs_login(res) and cookies
            and cookie_send_allowed(target, _COOKIE_DOMAINS)):
        res = fetch_page(target, cookies=cookies, cookie_domains=_COOKIE_DOMAINS,
                         max_bytes=settings.html_download_limit)
        login_state_used = True
    if _needs_login(res):
        raise PlatformError(
            "login_required",
            "知乎页面要求登录校验，自动读取暂不可用；链接已保存。"
            "请粘贴要保存的正文，或补充截图。",
        )
    if res.status_code >= 400:
        raise status_error(
            res.status_code,
            deleted="该回答或文章已删除或不可见；如仍存在，可粘贴正文或补充截图。",
            blocked="知乎暂时拒绝了这次读取（风控或限流）；链接已保存，可稍后点「重新提取」，"
                    "也可以先粘贴正文或补充截图。",
        )

    body = res.content.decode("utf-8", errors="replace")
    raw = res.content

    initial = _load_initial_data(body)
    ent = None
    if content_type == "zhihu_answer" and initial:
        ent = _dig_entity(initial, "answers", content_id)
    elif content_type == "zhihu_article" and initial:
        ent = _dig_entity(initial, "articles", content_id)

    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    blocks: list[tuple[str, str]] = []
    image_urls: list[str] = []
    question_only = False
    partial_body = False
    warnings: list[str] = []

    if ent is not None:
        title = _ent_str(ent, "title")
        author = _ent_author(ent)
        published_at = _ent_str(ent, "created", "updated", "published_at")
        content_html = _ent_str(ent, "content")
        if _ent_paid(ent):
            # 盐选/付费：只保存公开可见部分（摘要），不猜测付费正文；
            # coverage 必须是 partial_text，不能把摘要当全文（docs/18 §7.5 规则 4）
            content_html = _ent_str(ent, "excerpt") or content_html
            partial_body = True
            warnings.append("该内容为盐选/付费内容，仅保存公开可见部分。")
        if content_html:
            blocks, image_urls = _parse_content_html(content_html)
        if content_type == "zhihu_question":
            question_only = True
    elif content_type == "zhihu_question":
        # 问题页没有对应回答实体：保存问题标题，进入「请补充具体回答」
        q_ent = _dig_entity(initial, "questions", content_id) if initial else None
        if q_ent is None:
            m = re.search(r"<title[^>]*>([^<]{1,200})", body)
            if not m:
                raise PlatformError("structure_changed", "知乎页面结构已变化，无法读取问题标题")
            title = m.group(1).strip()
        else:
            title = _ent_str(q_ent, "title")
        question_only = True
        warnings.append("问题页包含多个回答；请打开具体回答再分享，或直接粘贴要保存的正文。")
    else:
        # 无 initialData：退回正文容器（RichContent / Post-RichText）
        blocks, image_urls, title, author = _container_fallback(body)
        if not blocks:
            raise PlatformError(
                "structure_changed",
                "知乎页面可访问但已知结构均未命中，无法提取正文；请粘贴正文或补充截图。",
            )

    segments = [
        {
            "segment_id": f"s{i:04d}",
            "text": text,
            "artifact_file_id": None,
            "locator": {"type": content_type, "content_id": content_id,
                        "kind": kind, "index": i},
            "origin": "platform_content",
            "confidence": None,
            "kind": kind,
        }
        for i, (kind, text) in enumerate(blocks, start=1)
    ]
    if not segments and not question_only:
        raise PlatformError(
            "empty_content",
            "该内容没有公开可见的正文（可能已删除、折叠或需要购买）；请粘贴正文或补充截图。",
        )

    images: list = []
    missing: list[str] = []
    if partial_body:
        missing.append("付费/盐选正文未保存：只保留了页面公开可见的部分内容。")
    if image_urls and include_images:
        images, img_missing = download_images(
            image_urls, referer="https://www.zhihu.com/",
            max_bytes_per_image=settings.max_image_bytes,
            max_total_bytes=settings.images_total_bytes,
        )
        missing.extend(img_missing)
        if len(image_urls) > len(images):
            warnings.append(f"已取得 {len(image_urls)} 张图片引用中的 {len(images)} 张；未取得的已列入缺失清单。")
    elif image_urls:
        warnings.append(f"内容含 {len(image_urls)} 张图片引用，本次未下载（默认不提取图片，需要时可在条目管理点「提取图片」重新提取）。")

    return ZhihuExtraction(
        content_type=content_type,
        content_id=content_id,
        question_id=question_id,
        answer_id=answer_id,
        canonical_url=res.url,
        title=title[:300] if title else None,
        author=author[:120] if author else None,
        published_at=published_at,
        raw_html=raw,
        raw_mime=res.mime or "text/html",
        segments=segments,
        image_urls=image_urls,
        images=images,
        missing_materials=missing,
        warnings=warnings,
        login_state_used=login_state_used,
        question_only=question_only,
        partial_body=partial_body,
    )


def _is_login_landing(url: str) -> bool:
    """最终落点是知乎的登录/人机校验页（只按 URL 结构判定，不猜正文文字）。"""
    path = (urlparse(url).path or "").rstrip("/")
    return any(path == p or path.startswith(p + "/") for p in _LOGIN_PATHS)


def _needs_login(res: object) -> bool:
    """登录墙判定：状态码、zse 挑战壳与登录落点 URL。

    不再用「请登录」+「查看全部」这类整页子串——正常回答里也会出现，
    命中等于是把已解析到的正文丢掉（审查 C-15）。
    """
    status = res.status_code  # type: ignore[attr-defined]
    if status in (401, 403):
        return True
    if _is_login_landing(res.url or ""):  # type: ignore[attr-defined]
        return True
    body = res.content.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
    return _is_challenge(status, body)


def _load_initial_data(body: str) -> dict | None:
    m = _INITIAL_DATA_RE.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _container_fallback(body: str) -> tuple[list[tuple[str, str]], list[str], str | None, str | None]:
    """无 initialData 时从已知正文容器提取（docs/18 §7.5 只认明确容器）。"""
    m = re.search(
        r'<div[^>]+class="[^"]*(?:RichContent-inner|Post-RichText)[^"]*"[^>]*>(.*?)</div>\s*</div>',
        body, re.S,
    )
    if not m:
        tm = re.search(r"<title[^>]*>([^<]{1,200})", body)
        return [], [], (tm.group(1).strip() if tm else None), None
    blocks, images = _parse_content_html(m.group(1))
    tm = re.search(r"<title[^>]*>([^<]{1,200})", body)
    return blocks, images, (tm.group(1).strip() if tm else None), None

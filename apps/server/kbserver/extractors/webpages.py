"""普通网页与公众号正文提取器（docs/02 §5.1 策略矩阵）。

规则：
- 所有网络访问经 security.safe_fetch，不绕过 DNS/IP/重定向/大小校验。
- 只处理用户明确提交的单个 URL；不抓取站内其他链接，不扩大采集范围。
- 公众号页面用已知公开结构（#activity-name/#js_content/#js_name/#publish_time）；
  其余网页用通用启发式（段落聚类选正文容器）。
- 原始 HTML 响应作为来源材料留存；不声称完整浏览器镜像，脚本/样式/动态
  内容未归档写入说明。正文图片限量下载，未取得的写入 missing_materials。
- 取不到正文（JS 渲染、登录墙、反爬页）时如实进入 needs_input，不伪造
  正文；失败状态区分 blocked / network_error / empty_content / unsupported。
- 没有依据的作者/日期留空，不用标题或猜测冒充原文（docs/02 §5.3）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from ..security.safe_fetch import SafeFetchError, safe_fetch

EXTRACTOR_VERSION = "webpage_article-1.0.0"

# 单条最多下载的正文图片数；其余如实记入缺失清单
MAX_CONTENT_IMAGES = 24

_URL_IN_TEXT = re.compile(r"https?://[^\s，,、）)】\]]+")

_IMG_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
    "image/avif": "avif",
}

_WECHAT_HOST_SUFFIX = "mp.weixin.qq.com"


class WebpageError(Exception):
    """提取失败。status: unsupported / blocked / network_error / empty_content。

    worker 据此决定重试（network_error）或进入补充材料（其余）。
    """

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class ImageDownload:
    """成功下载的一张正文图片。"""
    url: str
    mime: str
    ext: str
    data: bytes


@dataclass
class WebpageExtraction:
    """成功提取结果：元数据 + 原始 HTML + 统一片段 + 图片（发布前的全部材料）。"""
    platform: str  # "wechat_mp" | "web"
    canonical_url: str  # 重定向展开后的最终 URL
    title: str | None
    author: str | None
    published_at: str | None
    raw_html: bytes
    raw_mime: str
    segments: list[dict] = field(default_factory=list)
    images: list[ImageDownload] = field(default_factory=list)
    missing_materials: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> str:
        # 完成了本次页面静态正文范围（docs/02 §5.3；不含脚本/动态内容）
        return "full_text" if self.segments else "metadata_only"


def extract_first_url(share_text: str | None) -> str | None:
    """从分享文字提取第一条 URL（分享文字本身由 capture.json 留存）。"""
    if not share_text:
        return None
    m = _URL_IN_TEXT.search(share_text)
    return m.group(0).rstrip(".,;！!?？") if m else None


def page_slug(url: str) -> str:
    """URL 的确定性短标识，用于归档路径。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]


# ---- 容错 HTML 树 ----

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}
_BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "li", "dd", "dt",
               "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "figcaption", "main"}


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict | None = None, parent: "_Node | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []  # str | _Node
        self.parent = parent


class _HtmlTree(HTMLParser):
    """容错 HTML 树：不追求规范解析，只为正文启发式提供结构。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self.cur = self.root
        self.title_text: str | None = None
        self.metas: dict[str, str] = {}
        self._skip_stack: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_stack:
            if tag in _SKIP_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in _SKIP_TAGS:
            self._skip_stack.append(tag)
            return
        adict: dict[str, str] = {}
        for k, v in attrs:
            if k not in adict:
                adict[k] = v or ""
        if tag == "meta":
            key = adict.get("property") or adict.get("name")
            if key and adict.get("content"):
                self.metas.setdefault(key.lower(), adict["content"])
        if tag == "title":
            self._in_title = True
        node = _Node(tag, adict, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID_TAGS:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_stack or tag in _SKIP_TAGS:
            return
        if tag in _VOID_TAGS:
            self.handle_starttag(tag, attrs)
            return
        # 罕见的非 void 自闭合：当开标签 + 立即闭合
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        self._in_title = False
        if tag in _VOID_TAGS:
            return
        # 容错回溯：存在同名祖先则收拢到它，否则忽略该闭合
        node = self.cur
        depth = 0
        while node is not None and node.tag != "#root" and depth < 64:
            if node.tag == tag:
                self.cur = node.parent
                return
            node = node.parent
            depth += 1

    def handle_data(self, data):
        if self._skip_stack or not data:
            return
        if self._in_title:
            # <title> 只作为元数据记录，不进入正文树（避免 fallback 把它当正文）
            self.title_text = (self.title_text or "") + data
            return
        self.cur.children.append(data)


def _parse_html(content: bytes) -> _HtmlTree:
    tree = _HtmlTree()
    try:
        tree.feed(content.decode("utf-8", errors="replace"))
    except Exception as exc:  # 解析器对畸形输入一般不抛错，这里兜底归类
        raise WebpageError("blocked", f"页面解析失败：{exc}") from exc
    try:
        tree.close()
    except Exception:
        pass
    return tree


def _iter_nodes(node: _Node):
    yield node
    for c in node.children:
        if isinstance(c, _Node):
            yield from _iter_nodes(c)


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _text_of(node: _Node) -> str:
    parts: list[str] = []

    def walk(n: _Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                parts.append(c)
            else:
                if c.tag == "br":
                    parts.append(" ")
                walk(c)

    walk(node)
    return _clean_text("".join(parts))


def _find_by_id(root: _Node, node_id: str) -> _Node | None:
    for n in _iter_nodes(root):
        if n.attrs.get("id") == node_id:
            return n
    return None


def _leaf_blocks(root: _Node) -> list[_Node]:
    """叶子文本块：块级、有文字、后代中不再有带文字的块。文档顺序返回。"""
    order = list(_iter_nodes(root))
    texty: dict[int, bool] = {}  # id(node) -> 后代（含自身）是否存在带文字块
    leaves: list[_Node] = []
    for n in reversed(order):
        own = bool(n.tag in _BLOCK_TAGS and _text_of(n))
        child_any = any(texty.get(id(c), False) for c in n.children if isinstance(c, _Node))
        texty[id(n)] = own or child_any
        if own and not child_any:
            leaves.append(n)
    leaves.reverse()
    return leaves


def _depth(node: _Node) -> int:
    d = 0
    while node is not None and node.tag != "#root":
        d += 1
        node = node.parent
    return d


def _content_root(root: _Node, leaves: list[_Node]) -> _Node:
    """选包含 ≥60% 正文文字的最深容器；找不到就退回整个文档。"""
    if not leaves:
        return root
    total = sum(len(_text_of(l)) for l in leaves)
    if total <= 0:
        return root
    stats: dict[int, list[int]] = {}  # id -> [文字长度, 叶子数]
    nodes: dict[int, _Node] = {}
    for leaf in leaves:
        node = leaf.parent
        while node is not None:
            s = stats.setdefault(id(node), [0, 0])
            s[0] += len(_text_of(leaf))
            s[1] += 1
            nodes[id(node)] = node
            node = node.parent
    best: _Node | None = None
    best_depth = -1
    for key, (length, _count) in stats.items():
        if length / total < 0.6:
            continue
        d = _depth(nodes[key])
        if d > best_depth:
            best = nodes[key]
            best_depth = d
    return best if best is not None else root


def _blocks_under(container: _Node, leaves: list[_Node]) -> list[_Node]:
    ids = {id(n) for n in _iter_nodes(container)}
    return [l for l in leaves if id(l) in ids]


def _fallback_paragraphs(root: _Node) -> list[str]:
    """没有块级结构时（正文裸放在 body 下用 <br> 分行）按边界切段。"""
    paras: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        t = _clean_text("".join(buf))
        buf.clear()
        if t:
            paras.append(t)

    def walk(n: _Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                buf.append(c)
            elif c.tag == "br":
                flush()
            elif c.tag in _BLOCK_TAGS:
                flush()
                walk(c)
                flush()
            else:
                walk(c)

    walk(root)
    flush()
    return paras


def _collect_image_urls(container: _Node, base_url: str, *, wechat: bool) -> list[str]:
    urls: list[str] = []
    for n in _iter_nodes(container):
        if n.tag != "img":
            continue
        order = ("data-src", "data-original", "src") if wechat else ("src", "data-src", "data-original")
        raw = next((n.attrs.get(k) for k in order if n.attrs.get(k)), None)
        if not raw or raw.startswith("data:"):
            continue
        absu = urljoin(base_url, raw.strip())
        if absu.startswith(("http://", "https://")):
            urls.append(absu)
    return list(dict.fromkeys(urls))


# ---- 元数据 ----

def _is_wechat(url: str, tree: _HtmlTree) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host == _WECHAT_HOST_SUFFIX or host.endswith("." + _WECHAT_HOST_SUFFIX):
        return True
    return _find_by_id(tree.root, "js_content") is not None


def _normalize_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.isoformat()
    except ValueError:
        pass
    m = re.search(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})", raw)
    if not m:
        return None
    date = f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    tm = re.search(r"(\d{1,2}):(\d{2})", raw)
    if tm:
        return f"{date}T{int(tm[1]):02d}:{int(tm[2]):02d}:00"
    return date


# ---- 总入口 ----

def _browser_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def extract(url: str | None, *, share_text: str | None = None) -> WebpageExtraction:
    """提取一个网页/公众号文章的静态正文；失败抛 WebpageError。

    只处理用户指定的这一个页面。正文启发式基于静态 HTML：动态渲染页面
    取不到正文时抛 empty_content，由 worker 进入补充材料，不伪造内容。
    """
    from ..config import get_settings

    settings = get_settings()
    target = (url or "").strip() or extract_first_url(share_text)
    if not target:
        raise WebpageError("unsupported", "没有可处理的网页链接")
    if not target.lower().startswith(("http://", "https://")):
        raise WebpageError("unsupported", f"不是 HTTP(S) 链接：{target[:200]}")

    try:
        res = safe_fetch(target, max_bytes=settings.html_download_limit, timeout=20.0,
                         headers=_browser_headers())
    except SafeFetchError as exc:
        if exc.code == "NETWORK_ERROR":
            raise WebpageError("network_error", f"网页下载失败：{exc}") from exc
        raise WebpageError("blocked", f"网页下载被拒绝：{exc}") from exc

    if res.status_code >= 400:
        raise WebpageError("blocked", f"页面返回 HTTP {res.status_code}，无法读取正文")
    if not (res.mime.startswith("text/html") or res.mime.startswith("application/xhtml")):
        raise WebpageError(
            "blocked",
            f"页面不是 HTML（{res.mime}）；如是 PDF 或附件，请直接上传文件。",
        )

    tree = _parse_html(res.content)
    wechat = _is_wechat(res.url, tree)
    platform = "wechat_mp" if wechat else "web"
    container = tree.root  # 图片收集范围；无正文启发式结果时兜底为整个文档
    leaves = _leaf_blocks(tree.root)

    if wechat:
        content_node = _find_by_id(tree.root, "js_content")
        if content_node is None:
            raise WebpageError(
                "empty_content",
                "公众号页面未包含正文结构（可能需要登录或已被删除）。"
                "可复制正文粘贴保存，或补充截图。",
            )
        leaf_blocks = _blocks_under(content_node, leaves)
        paragraphs = [_text_of(b) for b in leaf_blocks if _text_of(b)]
        container = content_node
    else:
        paragraphs = []
        if leaves:
            container = _content_root(tree.root, leaves)
            paragraphs = [_text_of(b) for b in _blocks_under(container, leaves) if _text_of(b)]
        if len("".join(paragraphs)) < 40:
            paragraphs = _fallback_paragraphs(tree.root)
            container = tree.root
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        raise WebpageError(
            "empty_content",
            "未能从页面提取到正文：页面可能由脚本渲染或需要登录。"
            "可复制正文粘贴保存，或补充截图。",
        )

    segments = [
        {
            "segment_id": f"s{i:04d}",
            "text": p,
            "artifact_file_id": None,
            "locator": {"type": "paragraph", "index": i},
            "origin": "web_article",
            "confidence": None,
        }
        for i, p in enumerate(paragraphs, start=1)
    ]

    # 元数据：有依据才填
    title: str | None = None
    if wechat:
        n = _find_by_id(tree.root, "activity-name")
        if n and _text_of(n):
            title = _text_of(n)[:300]
    if title is None:
        title = _clean_text(tree.metas.get("og:title") or "")[:300] or None
    if title is None and tree.title_text:
        title = _clean_text(tree.title_text)[:300] or None

    author: str | None = None
    if wechat:
        n = _find_by_id(tree.root, "js_name")
        if n and _text_of(n):
            author = _text_of(n)[:120]
    if author is None:
        author = _clean_text(tree.metas.get("author") or "")[:120] or None

    published_at: str | None = None
    if wechat:
        n = _find_by_id(tree.root, "publish_time")
        published_at = _normalize_date(_text_of(n) if n else None)
    if published_at is None:
        published_at = _normalize_date(
            tree.metas.get("article:published_time") or tree.metas.get("pubdate") or tree.metas.get("date")
        )

    # 正文图片：限量下载；未取得的如实记入缺失清单（与 ItemOut 契约一致的字符串）
    images: list[ImageDownload] = []
    missing: list[str] = []
    warnings: list[str] = []
    image_urls = _collect_image_urls(container, res.url, wechat=wechat)
    for iu in image_urls:
        if len(images) >= MAX_CONTENT_IMAGES:
            missing.append(f"正文图片未下载（超出单条 {MAX_CONTENT_IMAGES} 张上限）：{iu}")
            continue
        try:
            ir = safe_fetch(iu, max_bytes=settings.max_image_bytes, timeout=20.0,
                            mime_prefixes=("image/",))
        except SafeFetchError as exc:
            missing.append(f"正文图片未取得：{iu}（{exc}）")
            continue
        if ir.status_code >= 400:
            missing.append(f"正文图片未取得：{iu}（HTTP {ir.status_code}）")
            continue
        images.append(ImageDownload(url=iu, mime=ir.mime,
                                    ext=_IMG_EXT.get(ir.mime, "img"), data=ir.content))
    if len(image_urls) > len(images):
        warnings.append(
            f"页面含 {len(image_urls)} 张正文图片，已取得 {len(images)} 张；"
            "未取得的已列入缺失清单。"
        )

    warnings.append("已留存原始 HTML 响应；页面脚本、样式与动态内容未归档，不构成完整网站镜像。")

    return WebpageExtraction(
        platform=platform,
        canonical_url=res.url,
        title=title,
        author=author,
        published_at=published_at,
        raw_html=res.content,
        raw_mime=res.mime,
        segments=segments,
        images=images,
        missing_materials=missing,
        warnings=warnings,
    )

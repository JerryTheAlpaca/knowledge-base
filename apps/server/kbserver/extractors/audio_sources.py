"""来源适配器分发（docs/13 §4）：页面/直链 → 通用远程或对象音频输入。

四种输入（docs/13 §1）：
- B 站视频音频：extractors/bilibili_audio（保留既有登录态/WBI/音轨规则）；
- 音频直链：DirectAudioAdapter（响应 MIME/格式探测确认，不靠后缀）；
- 静态网页音频：WebAudioAdapter（<audio>/<source>/og:audio/JSON-LD，多候选需选择）；
- 上传录音：ObjectAudioInput（服务端解析的已授权对象路径）。

顺序分发即可，不建设可动态安装的插件注册系统（docs/13 §3）。
"""
from __future__ import annotations

import hashlib
import json
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from sqlalchemy.orm import Session

from ..audio.types import (
    ACQ_UPLOADED,
    ACQ_WEB_STREAM,
    AudioSource,
    AudioSourceError,
    AudioSourceSelectionRequired,
    ObjectAudioInput,
    RemoteAudioInput,
    ResolvedAudioSource,
)
from ..domain.source_labels import (
    WEB_LIKE_PLATFORMS,
    default_media_kind,
    resolve_platform,
)
from ..models import AudioAsset, Capture, Item, SourceRevision, Upload
from ..security.safe_fetch import SafeFetchError, probe_url, safe_fetch
from ..storage.objects import ObjectStore
from . import bilibili as bili
from . import bilibili_audio as baudio
from . import webpages as webpage_util

EXTRACTOR_VERSION = "audio_sources-1.0.0"

_AUDIO_MIME_PREFIXES = ("audio/",)
_AUDIO_MIME_FALLBACK = (
    "application/ogg", "application/octet-stream", "video/mp4", "audio/mp4",
)
_HTML_MIME_PREFIXES = ("text/html", "application/xhtml")

_ADAPTER_DIRECT = "direct_audio"
_ADAPTER_WEB = "web_audio"
_ADAPTER_UPLOAD = "audio_upload"


def _generic_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }


def _candidate_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# ---- 静态网页音频候选解析（docs/13 §4.3）----

class _AudioScanParser(HTMLParser):
    """只收集明确音频信息；不执行脚本、不抓站内其他链接。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.groups: list[dict] = []          # [{title, entries: [{url, mime}]}]
        self._current: dict | None = None      # 当前 <audio> 元素分组
        self.metas: dict[str, str] = {}
        self.title: str | None = None
        self._in_title = False
        self._ldjson: list[str] = []
        self._in_ldjson = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        adict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "audio":
            self._current = {"title": None, "entries": []}
            self.groups.append(self._current)
            if adict.get("src"):
                self._add(adict["src"], adict.get("type"))
        elif tag == "source":
            if adict.get("src"):
                self._add(adict["src"], adict.get("type"))
        elif tag == "meta":
            key = adict.get("property") or adict.get("name")
            if key and adict.get("content"):
                self.metas.setdefault(key.lower(), adict["content"])
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            if (adict.get("type") or "").lower() == "application/ld+json":
                self._in_ldjson = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "audio":
            if self._current is not None and not self._current["entries"]:
                self.groups.pop()  # 无 src 的空 <audio> 不产出候选
            self._current = None
        elif tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ldjson = False

    def handle_data(self, data):
        if self._in_title and data.strip():
            self.title = ((self.title or "") + data).strip()[:300]
        if self._in_ldjson:
            self._ldjson.append(data)

    def _add(self, src: str, mime: str | None):
        if self._current is None:
            self._current = {"title": None, "entries": []}
            self.groups.append(self._current)
            self._current = None
        self.groups[-1]["entries"].append({"url": src, "mime": (mime or None)})

    @property
    def ldjson_docs(self) -> list:
        docs = []
        for raw in self._ldjson:
            try:
                docs.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
        return docs


def _ldjson_audio_urls(docs: list, max_nodes: int = 64) -> list[str]:
    """JSON-LD AudioObject.contentUrl；只取明确 @type=AudioObject 的节点。"""
    out: list[str] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 6 or len(out) >= max_nodes:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
            return
        if not isinstance(node, dict):
            return
        types = node.get("@type")
        type_list = types if isinstance(types, list) else [types]
        if any(str(t).lower() == "audioobject" for t in type_list):
            content = node.get("contentUrl") or node.get("embedUrl")
            if isinstance(content, str) and content.strip():
                out.append(content.strip())
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, depth + 1)

    for doc in docs:
        walk(doc)
    return list(dict.fromkeys(out))


def parse_audio_candidates(html: bytes, base_url: str) -> list[dict]:
    """静态 HTML → 逻辑音频候选列表；同一 <audio> 下的多格式编码合并为一条。"""
    parser = _AudioScanParser()
    try:
        parser.feed(html.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:  # noqa: BLE001 —— 畸形页面按无候选处理
        return []

    groups: list[dict] = []
    for group in parser.groups:
        urls: list[str] = []
        mimes: list[str | None] = []
        for entry in group["entries"]:
            url = urljoin(base_url, entry["url"].strip())
            if not url.startswith(("http://", "https://")):
                continue
            urls.append(url)
            mimes.append(entry.get("mime"))
        if urls:
            groups.append({"urls": urls, "mimes": mimes})

    # og:audio / JSON-LD 是页面级音频信号：避免与已解析元素重复
    seen = {u for g in groups for u in g["urls"]}
    for extra in ([parser.metas.get("og:audio")] + _ldjson_audio_urls(parser.ldjson_docs)):
        if not extra:
            continue
        url = urljoin(base_url, extra.strip())
        if url.startswith(("http://", "https://")) and url not in seen:
            groups.append({"urls": [url], "mimes": [None]})
            seen.add(url)

    title = parser.title or parser.metas.get("og:title")
    out: list[dict] = []
    for group in groups:
        url = group["urls"][0]
        out.append({
            "candidate_id": _candidate_id(url),
            "url": url,
            "title": title,
            "mime_hint": group["mimes"][0],
            "formats": [u for u in group["urls"]],
            "duration_hint": None,
        })
    return out


# ---- 输入构造 ----

def _upload_object_input(db: Session, item: Item, payload: dict,
                         store: ObjectStore) -> ResolvedAudioSource:
    """上传录音：解析服务端登记的原件引用，绝不接受客户端传入的路径/Key。"""
    asset = (
        db.query(AudioAsset)
        .filter(AudioAsset.user_id == item.user_id, AudioAsset.item_id == item.id,
                AudioAsset.retention_state == "retained")
        .order_by(AudioAsset.created_at.desc())
        .first()
    )
    upload: Upload | None = None
    if asset is not None:
        upload = db.get(Upload, asset.upload_id)
    if upload is None:
        uid = payload.get("primary_audio_upload_id")
        if uid:
            upload = db.query(Upload).filter(Upload.user_id == item.user_id, Upload.id == uid).one_or_none()
    if upload is None:
        raise AudioSourceError("audio_source_unsupported", "条目没有可用的上传录音原件。")
    if upload.state != "completed":
        raise AudioSourceError("audio_source_unsupported", "上传录音尚未完成，无法转写。")

    path = store.object_path(upload.storage_key)
    if not path.exists():
        raise AudioSourceError("network_error", "上传原件在对象存储中缺失，需重新上传。")
    source = AudioSource(
        platform="audio_upload",
        media_kind="audio",
        adapter_id=_ADAPTER_UPLOAD,
        adapter_version=EXTRACTOR_VERSION,
        original_url=None,
        canonical_url=None,
        title=(asset.filename if asset else None) or upload.filename or None,
        author=None,
        source_locator={"type": "uploaded_audio", "upload_id": upload.id,
                        "filename": upload.filename, "sha256": upload.sha256},
        duration_hint=None,
        acquisition=ACQ_UPLOADED,
    )
    return ResolvedAudioSource(
        source=source,
        input=ObjectAudioInput(source=source, path=path, size_bytes=upload.bytes,
                               sha256=upload.sha256),
        input_fingerprint=f"upload:{upload.sha256}",
    )


def _bilibili_resolved(db: Session, item: Item, payload: dict, settings) -> ResolvedAudioSource:
    from ..workers.asr import _user_sessdata  # 延迟导入避免循环依赖

    sessdata, sess_err = _user_sessdata(db, item.user_id)
    if sess_err:
        raise AudioSourceError("login_required", sess_err)
    target = (payload.get("original_url") or "").strip() or bili.extract_first_url(payload.get("share_text"))
    if not target:
        raise AudioSourceError("audio_source_unsupported", "条目中没有 B 站链接。")
    ref = bili.resolve_share_url(target)
    page = bili.resolve_video_part(ref, settings.subtitle_download_limit)
    time.sleep(bili.REQUEST_GAP_S)  # 与字幕路径同节奏，避免连续打接口
    stream = baudio.resolve_audio_stream(ref, page, sessdata=sessdata,
                                        max_bytes=settings.subtitle_download_limit)
    inp = baudio.bilibili_audio_input(ref, page, stream)
    return ResolvedAudioSource(
        source=inp.source, input=inp,
        input_fingerprint=baudio.source_fingerprint(ref, page, stream),
    )


def _web_direct_or_page(url: str, settings, selection: str | None) -> ResolvedAudioSource:
    """先探针判断直链或网页；网页解析出音频候选（多候选需选择）。"""
    try:
        probe = probe_url(url, headers=_generic_headers(), sample_bytes=64 * 1024)
    except SafeFetchError as exc:
        if exc.code in ("NETWORK_ERROR", "CANCELLED"):
            raise AudioSourceError("network_error", f"音频地址探测失败：{exc}") from exc
        raise AudioSourceError("blocked", f"音频地址被拒绝：{exc}") from exc

    if probe.status_code >= 400:
        raise AudioSourceError("network_error", f"音频地址返回 HTTP {probe.status_code}")

    mime = probe.mime or ""
    if mime.startswith(_AUDIO_MIME_PREFIXES) or (
        mime in _AUDIO_MIME_FALLBACK and not mime.startswith(_HTML_MIME_PREFIXES)
    ):
        source = AudioSource(
            platform="web", media_kind="audio", adapter_id=_ADAPTER_DIRECT,
            adapter_version=EXTRACTOR_VERSION, original_url=url, canonical_url=None,
            title=None, author=None, duration_hint=None,
            # 持久化只用采集定位与主机名：带时效签名的 CDN 地址不作永久身份
            source_locator={"type": "web_direct_audio", "mime": mime,
                            "host": urlparse(probe.url).hostname},
            acquisition=ACQ_WEB_STREAM,
        )
        inp = RemoteAudioInput(source=source, urls=[probe.url, url],
                               headers=_generic_headers(), duration_hint=None)
        return ResolvedAudioSource(
            source=source, input=inp,
            input_fingerprint=f"web-direct:{urlparse(url).hostname}:{_candidate_id(url)}",
        )

    if not mime.startswith(_HTML_MIME_PREFIXES):
        raise AudioSourceError(
            "audio_source_unsupported",
            f"该地址不是音频也不是网页（{mime or '未知类型'}），无法转写。",
        )

    try:
        page = safe_fetch(probe.url, max_bytes=settings.html_download_limit, timeout=20.0,
                          headers=_generic_headers())
    except SafeFetchError as exc:
        if exc.code == "NETWORK_ERROR":
            raise AudioSourceError("network_error", f"页面下载失败：{exc}") from exc
        raise AudioSourceError("blocked", f"页面下载被拒绝：{exc}") from exc

    candidates = parse_audio_candidates(page.content, page.url)
    if not candidates:
        raise AudioSourceError(
            "audio_source_unsupported",
            "页面没有可静态解析的音频（可能需要脚本或登录）；可上传音频文件后转写。",
        )
    if len(candidates) > 1:
        if selection is None:
            raise AudioSourceSelectionRequired(candidates)
        chosen = next((c for c in candidates if c["candidate_id"] == selection), None)
        if chosen is None:
            # 页面已变化：不信任客户端提交的新地址，要求重新选择
            raise AudioSourceSelectionRequired(candidates)
        candidate = chosen
    else:
        candidate = candidates[0]

    source = AudioSource(
        platform="web", media_kind="audio", adapter_id=_ADAPTER_WEB,
        adapter_version=EXTRACTOR_VERSION, original_url=url, canonical_url=page.url,
        title=candidate.get("title"), author=None,
        duration_hint=candidate.get("duration_hint"),
        source_locator={"type": "web_page_audio", "page_url": page.url,
                        "candidate_id": candidate["candidate_id"],
                        "media_url": candidate["url"]},
        acquisition=ACQ_WEB_STREAM,
    )
    inp = RemoteAudioInput(source=source, urls=[candidate["url"]],
                           headers=_generic_headers(),
                           duration_hint=candidate.get("duration_hint"))
    return ResolvedAudioSource(
        source=source, input=inp,
        input_fingerprint=f"web-page:{page.url}#{candidate['candidate_id']}",
    )


def resolve_audio_source(db: Session, item: Item, *, selection: str | None = None,
                         store: ObjectStore | None = None) -> ResolvedAudioSource:
    """解析条目的音频来源并返回通用输入；失败抛 AudioSourceError。

    - B 站：播放接口独立音轨（沿用现有登录态/音轨规则）；
    - 上传录音：服务端登记的已授权对象原件；
    - 普通网页/音频直链：静态解析或直链探测，多候选时抛选择异常。
    """
    from ..config import get_settings

    settings = get_settings()
    store = store or ObjectStore()
    capture = db.get(Capture, item.capture_id)
    payload = (capture.input_json or {}) if capture else {}
    source = (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id, SourceRevision.revision == item.source_revision)
        .one_or_none()
    )
    meta = source.metadata_json if source else {}
    url = (payload.get("original_url") or "").strip() or webpage_util.extract_first_url(payload.get("share_text"))
    # 渠道取值（旧客户端的 source_hint=web_inbox）不能当平台：回退按 URL 推断
    platform = resolve_platform(meta.get("platform") or payload.get("source_hint"), url)
    media_kind = meta.get("media_kind") or _guess_media_kind(payload, meta)

    if platform == "bilibili":
        return _bilibili_resolved(db, item, payload, settings)
    if platform == "audio_upload" or (media_kind == "audio" and _has_primary_upload(payload)):
        return _upload_object_input(db, item, payload, store)
    if url and platform in WEB_LIKE_PLATFORMS:
        return _web_direct_or_page(url, settings, selection)
    if media_kind == "audio" and payload.get("upload_ids"):
        return _upload_object_input(db, item, payload, store)
    raise AudioSourceError(
        "audio_source_unsupported",
        "该条目没有可转写的音频来源（需 B 站视频、网页音频、音频直链或上传录音）。",
    )


def _has_primary_upload(payload: dict) -> bool:
    return bool(payload.get("primary_audio_upload_id"))


def _guess_media_kind(payload: dict, meta: dict) -> str:
    """按输入意图推断 media_kind；无法确认是否音频的旧 web 记录保持 text。"""
    if payload.get("processing_intent") == "transcribe_audio":
        if payload.get("primary_audio_upload_id"):
            return "audio"
        return "audio" if payload.get("input_kind") == "audio" else "text"
    if payload.get("input_kind") == "audio":
        return "audio"
    return default_media_kind(meta.get("platform"), has_audio=False)


def candidate_list(candidates: list[dict]) -> list[dict]:
    """对外选择列表：不暴露任何客户端可伪造的请求头或本地路径。"""
    out = []
    for c in candidates:
        out.append({
            "candidate_id": c["candidate_id"],
            "title": c.get("title"),
            "duration_hint": c.get("duration_hint"),
            "mime_hint": c.get("mime_hint"),
            "host": urlparse(c.get("url") or "").hostname,
        })
    return out


def audio_capability(db: Session, item: Item) -> tuple[bool, str]:
    """条目是否具备可转写的音频来源（不触网的能力判断，供 API 显示按钮/校验）。

    B 站、上传录音、带链接的普通网页都视为**可能**支持；实际能否取得音频由
    prepare 阶段决定（可能是 audio_source_unsupported / 需要选择）。
    """
    capture = db.get(Capture, item.capture_id)
    payload = (capture.input_json or {}) if capture else {}
    source = (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id, SourceRevision.revision == item.source_revision)
        .one_or_none()
    )
    meta = source.metadata_json if source else {}
    url = (payload.get("original_url") or "").strip() or webpage_util.extract_first_url(payload.get("share_text"))
    platform = resolve_platform(meta.get("platform") or payload.get("source_hint"), url)
    media_kind = meta.get("media_kind") or _guess_media_kind(payload, meta)
    if platform == "bilibili":
        return True, "bilibili"
    if platform == "audio_upload" or payload.get("primary_audio_upload_id") or media_kind == "audio":
        return True, "upload"
    if platform in WEB_LIKE_PLATFORMS and url:
        return True, "web"
    return False, ""

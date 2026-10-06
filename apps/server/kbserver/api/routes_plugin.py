"""Obsidian 插件下载（docs/26）。

GET /downloads/golden-rose-inbox.zip：把 plugin_dist/ 下的三个文件打成 zip。
供设置页与新手指导的「下载插件」入口使用，公开可取（不需登录）：
拿到 zip 的人本来也要用自己的账号在插件里登录才有数据。

产物是纯文本文件、随服务端代码一起入库（构建脚本见
apps/obsidian-plugin/scripts/sync-dist.mjs）：api 镜像的 build context 只有
apps/server，服务器上也没有 node，部署时无法现场跑 esbuild。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, Response

from ..domain.errors import ApiError

router = APIRouter(tags=["plugin-download"])

PLUGIN_DIST_DIR = Path(__file__).resolve().parents[1] / "plugin_dist"
PLUGIN_ID = "golden-rose-inbox"
# Obsidian 只认这三个：入口、清单、样式。少一个插件都加载不起来。
PLUGIN_FILES = ("manifest.json", "main.js", "styles.css")

# 产物按 mtime 判缓存；只缓存读到的 parts，zip 每次现打（几十 KB，代价可忽略）
_CACHE: dict[str, object] = {}


def _read_dist() -> dict[str, bytes]:
    """读发布产物并按 mtime+size 判缓存：改插件重新入库后无需重启进程。"""
    try:
        parts = {name: (PLUGIN_DIST_DIR / name).read_bytes() for name in PLUGIN_FILES}
    except FileNotFoundError as exc:
        raise ApiError("NOT_FOUND",
                       "插件发布产物缺失，请联系站点管理员。") from exc
    stamp = tuple((PLUGIN_DIST_DIR / n).stat().st_mtime_ns for n in PLUGIN_FILES)
    if _CACHE.get("stamp") != stamp:
        _CACHE.clear()
        _CACHE["stamp"] = stamp
        _CACHE["parts"] = parts
    return parts


def _manifest_info(parts: dict[str, bytes]) -> dict:
    try:
        return json.loads(parts["manifest.json"].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError("PLUGIN_DIST_BROKEN",
                       "插件清单无法解析，请联系站点管理员。",
                       status_code=500) from exc


@router.get(f"/downloads/{PLUGIN_ID}.zip", include_in_schema=False)
def download_plugin() -> Response:
    """现打 zip：内容随发布产物变，不需要在仓库里存二进制。

    zip 内根目录是插件 id 命名的文件夹——用户把它解压到
    <库>/.obsidian/plugins/ 下就能直接被 Obsidian 认出来。
    """
    parts = _read_dist()
    manifest = _manifest_info(parts)
    buf = io.BytesIO()
    # 固定时间戳：同样的产物打出同样的字节，浏览器/CDN 缓存才有意义
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in PLUGIN_FILES:
            info = zipfile.ZipInfo(f"{PLUGIN_ID}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, parts[name])
    payload = buf.getvalue()
    version = manifest.get("version", "")
    filename = f"{PLUGIN_ID}-{version}.zip" if version else f"{PLUGIN_ID}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            # 带版本的插件不常变，但重新发布后要能立刻拿到；304 交给 ETag
            "Cache-Control": "public, max-age=300",
            "ETag": f'W/"{version}-{len(payload)}"',
            "X-Content-Type-Options": "nosniff",
        },
    )

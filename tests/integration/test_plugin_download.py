"""插件下载端点（docs/26）。

盯三件事：zip 能打出来且结构是 Obsidian 认的（根目录带插件 id 的文件夹、
三个文件齐）、公开可取（不需登录）、产物与仓库里的发布目录一致。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kbserver.api.routes_plugin import PLUGIN_DIST_DIR, PLUGIN_FILES, PLUGIN_ID
from kbserver.app import create_app


@pytest.fixture()
def pc(engine):
    with TestClient(create_app()) as c:
        yield c


def test_release_reports_version(pc):
    r = pc.get("/v1/plugin/release")
    assert r.status_code == 200
    info = r.json()
    assert info["plugin_id"] == PLUGIN_ID
    assert info["version"]  # 版本号取自 manifest.json
    assert info["download_url"] == f"/downloads/{PLUGIN_ID}.zip"


def test_zip_is_observable_without_login(pc):
    """未登录也要能下：安装发生在登录之前，登录是插件里的下一步。"""
    assert pc.get("/v1/plugin/release").status_code == 200
    r = pc.get(f"/downloads/{PLUGIN_ID}.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert PLUGIN_ID in r.headers["content-disposition"]


def test_zip_layout_matches_obsidian_expectation(pc):
    """Obsidian 只认 <库>/.obsidian/plugins/<id>/{main.js,manifest.json,styles.css}。

    少一个文件插件就加载不出来，根目录少一层文件夹用户就得自己再套一层。
    """
    r = pc.get(f"/downloads/{PLUGIN_ID}.zip")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = sorted(zf.namelist())
        assert names == sorted(f"{PLUGIN_ID}/{n}" for n in PLUGIN_FILES)
        assert zf.testzip() is None  # 每个条目的 CRC 都能校验
        manifest = json.loads(zf.read(f"{PLUGIN_ID}/manifest.json").decode("utf-8"))
    # 目录名必须与 manifest.id 一致，否则 Obsidian 认不出来
    assert manifest["id"] == PLUGIN_ID
    assert manifest["version"]


def test_zip_ships_current_build(pc):
    """zip 里的 main.js 与发布目录逐字节相同：不会发出过期版本。"""
    r = pc.get(f"/downloads/{PLUGIN_ID}.zip")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        shipped = zf.read(f"{PLUGIN_ID}/main.js")
    assert shipped == (PLUGIN_DIST_DIR / "main.js").read_bytes()


def test_dist_version_matches_plugin_manifest():
    """改了插件 manifest 却忘了 `npm run release`：线上会发旧版本号。

    发布产物与插件源码在同一个 commit 里（见 docs/26 §2），这条盯的就是
    忘记跑同步脚本的情况。
    """
    plugin_manifest = Path(__file__).resolve().parents[2] / "apps" / "obsidian-plugin" / "manifest.json"
    if not plugin_manifest.exists():  # 只装了服务端、没带插件源码的部署环境
        pytest.skip("仓库里没有插件源码")
    src = json.loads(plugin_manifest.read_text(encoding="utf-8"))
    dist = json.loads((PLUGIN_DIST_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert dist["version"] == src["version"]
    assert dist["id"] == src["id"] == PLUGIN_ID

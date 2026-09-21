"""分享站点公开访问、撤销与对象引用生命周期（docs/20 §9.4、§9.5、§13.2～§13.4）。

覆盖验收 A24、A25、A26、A32 的自动化部分。
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kbserver.config import get_settings
from kbserver.models import (
    BundleRevision,
    Item,
    ShareArtifact,
    ShareRevision,
    ShareWork,
    utcnow,
)
from kbserver.storage.objects import ObjectStore
from kbserver.workers import worker as worker_mod

from tests.integration.test_shares_api import (  # 复用同一套替身与环境夹具
    create,
    share_env,  # noqa: F401
    to_ready,
    write_result,  # noqa: F401
)


@pytest.fixture(autouse=True)
def _share_on(monkeypatch, tmp_path):
    """分享功能在测试里开启；spool 用临时目录，runner 排队不等待。"""
    from tests.integration.test_share_worker import FakeProvider
    from kbserver.workers import share as share_worker

    FakeProvider.calls = []
    FakeProvider.next_docs = []
    monkeypatch.setattr(share_worker, "OpenAICompatibleProvider", FakeProvider)
    monkeypatch.setenv("SHARE_ENABLED", "true")
    monkeypatch.setenv("SHARE_RUNNER_POLL_SECONDS", "0")
    monkeypatch.setenv("SHARE_SPOOL_DIR", str(tmp_path / "spool"))


@pytest.fixture()
def published(client, share_env, monkeypatch):
    env = share_env["a"]
    monkeypatch.setenv("SHARE_PUBLIC_BASE_URL", "https://share.example")
    share_id, run_id = create(client, env)
    to_ready(client, env, share_id, run_id)
    with share_env["session_factory"]() as db:
        revision_id = db.get(ShareWork, share_id).latest_ready_revision_id
    resp = client.post(f"/v1/shares/{share_id}/publish", json={"revision_id": revision_id},
                       headers={**env["headers"], "Idempotency-Key": "pub-1"})
    assert resp.status_code == 200, resp.text
    return {"share_id": share_id, "url": resp.json()["url"], "env": env, "db_env": share_env}


def _app():
    from kbserver.app import create_app

    return create_app()


def _public_client(base="https://share.example") -> TestClient:
    """分享站点的入口：只带分享路径，不带主站 Cookie。"""
    from kbserver.app import create_app

    return TestClient(create_app(), base_url=base)


def test_public_link_serves_isolated_page_with_no_store(published):
    token = published["url"].rsplit("/s/", 1)[1]
    client = _public_client()
    resp = client.get(f"/s/{token}")
    assert resp.status_code == 200
    assert 'sandbox="allow-scripts"' in resp.text
    assert "allow-same-origin" not in resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in {k.lower() for k in resp.headers}


def test_public_link_on_main_host_is_not_served(published):
    """主站域名不执行分享路由：不出现「主站直接跑生成页面」的退路。"""
    token = published["url"].rsplit("/s/", 1)[1]
    resp = _public_client("https://kb.example").get(f"/s/{token}")
    assert resp.status_code == 404
    assert "这个链接已经不可用" in resp.text
    assert "两篇材料的对照" not in resp.text  # 不可用页面不泄露标题


def test_revoke_then_republish_changes_link(published, client):
    """A26：旧链接不可用，重新发布使用新链接。"""
    public = _public_client()
    token = published["url"].rsplit("/s/", 1)[1]
    env = published["env"]
    assert public.get(f"/s/{token}").status_code == 200
    revoked = client.post(f"/v1/shares/{published['share_id']}/revoke", json={},
                         headers=env["headers"])
    assert revoked.json()["status"] == "revoked"
    assert public.get(f"/s/{token}").status_code == 404
    with published["db_env"]["session_factory"]() as db:
        revision_id = db.get(ShareWork, published["share_id"]).latest_ready_revision_id
    again = client.post(f"/v1/shares/{published['share_id']}/publish",
                        json={"revision_id": revision_id},
                        headers={**env["headers"], "Idempotency-Key": "pub-2"})
    new_token = again.json()["url"].rsplit("/s/", 1)[1]
    assert new_token != token
    assert public.get(f"/s/{new_token}").status_code == 200
    assert public.get(f"/s/{token}").status_code == 404


def test_preview_token_expiry_and_deletion(published, monkeypatch):
    env = published["env"]
    client = TestClient(_app())
    token = client.post(f"/v1/shares/{published['share_id']}/revisions/1/preview-token",
                        headers=env["headers"]).json()["preview_token"]
    public = _public_client()
    assert public.get(f"/preview/{token}").status_code == 200
    assert public.get(f"/preview/{token[:-2]}xx").status_code == 404
    # 删除作品立即使此前预览链接失效
    client.delete(f"/v1/shares/{published['share_id']}", headers=env["headers"])
    assert public.get(f"/preview/{token}").status_code == 404


def test_shared_physical_object_survives_deleting_one_work(client, share_env, monkeypatch):
    """A25：删除一个引用共享物理对象的作品，其他作品和原材料仍可用。"""
    env = share_env["a"]
    store = ObjectStore()
    first_id, first_run = create(client, env)
    to_ready(client, env, first_id, first_run)
    second_id, second_run = create(client, env, instructions="再做一个版本")
    to_ready(client, env, second_id, second_run)
    with share_env["session_factory"]() as db:
        keys_a = {a.storage_key for a in db.query(ShareArtifact).filter_by(work_id=first_id).all()}
        keys_b = {a.storage_key for a in db.query(ShareArtifact).filter_by(work_id=second_id).all()}
        shared = keys_a & keys_b
        assert shared, "两次创作应复用同一份固定快照对象"
    client.delete(f"/v1/shares/{first_id}", headers=env["headers"])
    with share_env["session_factory"]() as db:
        # 删除已过宽限期的作品，其引用才进入回收
        db.get(ShareWork, first_id).deleted_at = utcnow() - timedelta(days=2)
        db.commit()
    stats = worker_mod.retention_sweep(share_env["session_factory"], store)
    assert stats["released"] >= 1
    with share_env["session_factory"]() as db:
        for key in shared:
            assert db.query(ShareArtifact).filter_by(storage_key=key).first() is not None
            assert store.object_exists(key), f"仍被引用的物理对象被误删：{key}"
        second = db.get(ShareRevision, db.get(ShareWork, second_id).latest_ready_revision_id)
        assert store.object_exists(second.html_key)


def test_bundle_expiry_does_not_delete_share_referenced_manifest(client, share_env, monkeypatch):
    """A24：源 Bundle 到期与分享快照并存时，被持有材料不被误删。"""
    env = share_env["a"]
    store = ObjectStore()
    share_id, run_id = create(client, env)
    to_ready(client, env, share_id, run_id)
    with share_env["session_factory"]() as db:
        item = db.query(Item).filter_by(user_id=env["user_id"]).first()
        sha, key, size = store.put_bytes(b'{"files": []}')
        bundle = BundleRevision(item_id=item.id, user_id=env["user_id"], revision=1,
                                source_revision=1, manifest_key=key, manifest_sha256=sha,
                                processing_state="original_only",
                                expires_at=utcnow() - timedelta(days=99))
        db.add(bundle)
        # 分享快照复用了同一物理对象（内容寻址去重）
        db.add(ShareArtifact(user_id=env["user_id"], work_id=share_id, role="asset",
                             storage_key=key, sha256=sha, bytes=size, mime="application/json"))
        db.commit()
    stats = worker_mod.retention_sweep(share_env["session_factory"], store)
    assert stats["expired_bundles"] == 0
    assert store.object_exists(key)
    with share_env["session_factory"]() as db:
        assert db.query(BundleRevision).filter_by(manifest_key=key).first() is not None


def test_spool_temp_directories_are_swept(share_env, tmp_path):
    settings = get_settings()
    root = Path(settings.share_spool_dir)
    stale = root / ".tmp" / "task-old"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "marker").write_text("x", encoding="utf-8")
    import os
    old = (utcnow() - timedelta(hours=48)).timestamp()
    os.utime(stale / "marker", (old, old))
    os.utime(stale, (old, old))
    from kbserver.workers import share_retention

    removed = share_retention._sweep_spool(settings, utcnow())
    assert removed >= 1
    assert not stale.exists()

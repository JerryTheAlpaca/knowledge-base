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
    ProviderOperation,
    ShareArtifact,
    ShareConversation,
    ShareMessage,
    ShareRevision,
    ShareRun,
    ShareWork,
    new_id,
    utcnow,
)
from kbserver.repositories import shares as repo
from kbserver.storage.objects import ObjectStore
from kbserver.workers import worker as worker_mod

from tests.integration.test_shares_api import (  # 复用同一套替身与环境夹具
    add_revision,
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


def _unique_html(label: str) -> bytes:
    """每个用例自己的成品字节：对象按内容寻址，共用一段 HTML 会让「物理对象真的没了」
    这种断言被别的用例的引用挡住，测不出回收。"""
    return f'<!doctype html><main>{label} {new_id()}</main>'.encode("utf-8")


def test_superseded_revision_beyond_retention_is_reclaimed(client, share_env):
    """C-05：被新版本取代、又过了保留期的旧版本，产物要真的能回收。

    原来这条分支根本不存在（保留期只用来「保护」），私有存储因此只增不减。
    用例会先证明保留期内不动，再把旧版本推过 share_revision_retention_days。
    """
    env = share_env["a"]
    store = ObjectStore()
    share_id, run_id = create(client, env)
    old_html = _unique_html("第一版")
    to_ready(client, env, share_id, run_id, html=old_html)
    with share_env["session_factory"]() as db:
        work = db.get(ShareWork, share_id)
        old_rev_id = work.latest_ready_revision_id
        old_html_key = db.get(ShareRevision, old_rev_id).html_key
        old_run_id = db.get(ShareRevision, old_rev_id).run_id
        used_before = repo.user_storage_bytes(db, env["user_id"])
    new_html = _unique_html("第二版")
    add_revision(client, env, share_id, html=new_html)
    with share_env["session_factory"]() as db:
        latest_rev_id = db.get(ShareWork, share_id).latest_ready_revision_id
        assert latest_rev_id != old_rev_id
        # 旧版本还在保留期内：谁都不该动
        worker_mod.retention_sweep(share_env["session_factory"], store)
        assert db.query(ShareArtifact).filter_by(revision_id=old_rev_id, role="html").count() == 1
        assert store.object_exists(old_html_key)
    # 版本与产出它的那次任务一起变旧
    stale = utcnow() - timedelta(days=get_settings().share_revision_retention_days + 1)
    with share_env["session_factory"]() as db:
        db.get(ShareRevision, old_rev_id).created_at = stale
        db.get(ShareRun, old_run_id).updated_at = stale
        db.commit()
    worker_mod.retention_sweep(share_env["session_factory"], store)
    with share_env["session_factory"]() as db:
        assert db.query(ShareArtifact).filter_by(revision_id=old_rev_id).count() == 0
        assert not store.object_exists(old_html_key), "旧版本 HTML 对象没被回收"
        latest = db.get(ShareRevision, latest_rev_id)
        assert store.object_exists(latest.html_key), "当前草稿被连带删掉了"
        assert db.query(ShareArtifact).filter_by(revision_id=latest_rev_id, role="html").count() == 1
        # 用户已用存储随之下降（配额判定用的就是这个数）
        assert repo.user_storage_bytes(db, env["user_id"]) < used_before


def test_published_and_latest_revisions_survive_retention_age(client, share_env, monkeypatch):
    """已发布版本与最新草稿不受保留期影响：只有「被取代且过期」的那批才走。"""
    monkeypatch.setenv("SHARE_PUBLIC_BASE_URL", "https://share.example")
    env = share_env["a"]
    store = ObjectStore()
    share_id, run_id = create(client, env)
    to_ready(client, env, share_id, run_id, html=_unique_html("已发布那版"))
    with share_env["session_factory"]() as db:
        work = db.get(ShareWork, share_id)
        published_rev_id = work.latest_ready_revision_id
        published_html = db.get(ShareRevision, published_rev_id).html_key
        published_run_id = db.get(ShareRevision, published_rev_id).run_id
    resp = client.post(f"/v1/shares/{share_id}/publish", json={"revision_id": published_rev_id},
                       headers={**env["headers"], "Idempotency-Key": "pub-age"})
    assert resp.status_code == 200, resp.text
    add_revision(client, env, share_id, html=_unique_html("最新草稿"))
    stale = utcnow() - timedelta(days=get_settings().share_revision_retention_days + 1)
    with share_env["session_factory"]() as db:
        db.get(ShareRevision, published_rev_id).created_at = stale
        db.get(ShareRun, published_run_id).updated_at = stale
        db.commit()
    worker_mod.retention_sweep(share_env["session_factory"], store)
    with share_env["session_factory"]() as db:
        work = db.get(ShareWork, share_id)
        assert work.published_revision_id == published_rev_id
        assert store.object_exists(published_html), "已发布版本被回收了"
        assert db.query(ShareArtifact).filter_by(revision_id=published_rev_id).count() >= 1
        # 版本的依赖（整合稿、素材清单）登记在 run_id 上，也要跟着版本一起活着
        latest = db.get(ShareRevision, work.latest_ready_revision_id)
        assert store.object_exists(latest.synthesis_key)
        assert store.object_exists(latest.input_manifest_key)


def test_deleted_work_text_rows_purged_after_grace(client, share_env):
    """C-12：删除作品的宽限期过后，run/会话/消息/版本这些文本行也一起回收。

    宽限期内要还能回捞（对象与文本行都原样在），过期后 share_runs.request_text
    ——用户最初写下的原文——必须真的消失。
    """
    env = share_env["a"]
    store = ObjectStore()
    # 别人的作品先建好：保留期清理会把夹具里没有 Bundle 的材料文件当孤儿收走，
    # 之后再新建分享就取不到正文了。
    other_id, other_run = create(client, share_env["b"])
    to_ready(client, share_env["b"], other_id, other_run, html=_unique_html("别人的"))
    share_id, run_id = create(client, env)
    to_ready(client, env, share_id, run_id, html=_unique_html("要删掉的那版"))
    with share_env["session_factory"]() as db:
        revision_id = db.get(ShareWork, share_id).latest_ready_revision_id
        html_key = db.get(ShareRevision, revision_id).html_key
        request_text = db.get(ShareRun, run_id).request_text
        assert request_text and db.query(ShareMessage).filter_by(run_id=run_id).count() >= 1
        assert db.query(ProviderOperation).filter_by(share_run_id=run_id).count() >= 1
    assert client.delete(f"/v1/shares/{share_id}", headers=env["headers"]).status_code == 200
    # 宽限期内：作品看不见，但材料还在，删错了还能回捞
    stats = worker_mod.retention_sweep(share_env["session_factory"], store)
    assert stats["purged_works"] == 0
    with share_env["session_factory"]() as db:
        assert db.get(ShareRun, run_id).request_text == request_text
        assert db.query(ShareRevision).filter_by(id=revision_id).count() == 1
        assert store.object_exists(html_key)
    with share_env["session_factory"]() as db:
        db.get(ShareWork, share_id).deleted_at = utcnow() - timedelta(days=2)
        db.commit()
    stats = worker_mod.retention_sweep(share_env["session_factory"], store)
    assert stats["purged_works"] >= 1
    with share_env["session_factory"]() as db:
        assert db.get(ShareRun, run_id) is None
        assert db.query(ShareConversation).filter_by(work_id=share_id).count() == 0
        assert db.query(ShareMessage).filter_by(run_id=run_id).count() == 0
        assert db.query(ShareRevision).filter_by(work_id=share_id).count() == 0
        assert db.query(ShareArtifact).filter_by(work_id=share_id).count() == 0
        assert db.query(ProviderOperation).filter_by(share_run_id=run_id).count() == 0
        assert not store.object_exists(html_key)
        work = db.get(ShareWork, share_id)   # 作品行保留 tombstone，指针不再悬空
        assert work.deleted_at is not None and work.latest_ready_revision_id is None
        assert work.published_revision_id is None and work.active_run_id is None
    # 别人的作品不受牵连
    with share_env["session_factory"]() as db:
        assert db.get(ShareWork, other_id).latest_ready_revision_id is not None
        assert db.query(ShareRevision).filter_by(work_id=other_id).count() == 1
        assert db.query(ShareArtifact).filter_by(work_id=other_id).count() >= 1


def test_public_pages_require_share_enabled(published, monkeypatch):
    """C-11：SHARE_ENABLED=false 是总开关，公开页与短时预览都不交付。"""
    client = TestClient(_app())
    token = client.post(f"/v1/shares/{published['share_id']}/revisions/1/preview-token",
                        headers=published["env"]["headers"]).json()["preview_token"]
    public_token = published["url"].rsplit("/s/", 1)[1]
    public = _public_client()
    assert public.get(f"/s/{public_token}").status_code == 200
    assert public.get(f"/preview/{token}").status_code == 200
    monkeypatch.setenv("SHARE_ENABLED", "false")
    for path in (f"/s/{public_token}", f"/preview/{token}"):
        resp = public.get(path)
        assert resp.status_code == 404, path
        assert "这个链接已经不可用" in resp.text


def test_storage_key_index_comes_from_migrations(tmp_path):
    """新索引由 alembic 迁移真的建出来：跑完整迁移链到 head，不在测试里另搭一套 schema。

    其余用例走 Base.metadata.create_all，摸不到 migrations/versions/；这条在独立进程里
    对一个空库执行 `alembic upgrade head`（Settings.database_url 在导入期就定死了，
    同进程改环境变量到不了迁移），确认 SQLite 上这个索引真能建起来。
    """
    import os
    import sqlite3
    import subprocess
    import sys

    from kbserver import __file__ as kbserver_init

    server_dir = Path(kbserver_init).resolve().parents[1]      # apps/server
    db_path = tmp_path / "migrate-chain.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path.as_posix()}"}
    done = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
                          cwd=str(server_dir), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr[-2000:]

    conn = sqlite3.connect(db_path)
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='share_artifacts'")}
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        job_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        migration_tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        migration_cols = {row[1] for row in conn.execute("PRAGMA table_info(content_migrations)")}
        migration_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='content_migrations'").fetchone()[0]
    finally:
        conn.close()
    assert "ix_share_artifacts_storage_key" in names
    assert version == ("b5e9d3f7c2a8",)
    # v3 迁移基础设施同样由 alembic 建出来（docs/24 §7）：任务固定输入 + 转换台账
    assert "input_json" in job_cols
    assert "content_migrations" in migration_tables
    assert {"input_sha256", "converter_version", "new_bundle_revision", "status"} <= migration_cols
    assert "uq_content_migration_input" in migration_ddl

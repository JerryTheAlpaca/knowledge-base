# -*- coding: utf-8 -*-
"""M3 真机验收造数脚本（docs/06）：在本地服务器 SQLite 构造验收专用条目。

与生产数据完全隔离：KB_ACCEPT_DIR 指向临时数据目录（默认 %TEMP%/kb-m3-accept），
配合本地 uvicorn 实例使用。M5 隔离/恢复验收可复用。

用法：
  python tests/acceptance/seed_m3.py setup                                # 建表+用户/Token+vault data.json
  python tests/acceptance/seed_m3.py publish <key> [--title T]            # 发布新 bundle（bundle_published）
  python tests/acceptance/seed_m3.py evil-manifest <key> --evil-path P    # 篡改 manifest 路径（A16）
  python tests/acceptance/seed_m3.py evil-manifest <key> --evil-hash      # 篡改 manifest 哈希（A16）
  python tests/acceptance/seed_m3.py tombstone <key>                      # 标记删除（410 GONE，A21）
  python tests/acceptance/seed_m3.py receipts                             # 列出回执
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ACCEPT_DIR = Path(os.environ.get("KB_ACCEPT_DIR") or Path(os.environ.get("TEMP", ".")) / "kb-m3-accept")
SERVER_DIR = Path(__file__).resolve().parents[2] / "apps" / "server"

os.environ.setdefault("DATABASE_URL", f"sqlite:///{(ACCEPT_DIR / 'data' / 'db' / 'app.db').as_posix()}")
os.environ.setdefault("OBJECTS_DIR", str(ACCEPT_DIR / "data" / "objects"))
os.environ.setdefault("TMP_DIR", str(ACCEPT_DIR / "data" / "tmp"))
os.environ.setdefault("DELETIONS_DIR", str(ACCEPT_DIR / "data" / "deletions"))
os.environ.setdefault("PUBLIC_BASE_URL", "http://127.0.0.1:8000")

sys.path.insert(0, str(SERVER_DIR))

from kbserver.config import get_settings  # noqa: E402
from kbserver.db import make_engine, make_session_factory  # noqa: E402
from kbserver.models import (  # noqa: E402
    Base, BundleRevision, Capture, Device, Item, Receipt, SourceRevision, User, utcnow,
)
from kbserver.security.tokens import DESKTOP_SCOPES, issue_token  # noqa: E402
from kbserver.storage.objects import ObjectStore  # noqa: E402
from kbserver.domain.pipeline import canonical_json, publish_bundle, register_file  # noqa: E402

VAULT = ACCEPT_DIR / "vault"


def _session_factory():
    settings = get_settings()
    settings.ensure_dirs()
    return make_session_factory(make_engine(settings))


def cmd_setup(_args) -> None:
    settings = get_settings()
    settings.ensure_dirs()
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    with _session_factory()() as db:
        user = User(name="M3验收")
        db.add(user)
        db.flush()
        device = Device(user_id=user.id, kind="desktop", name="acceptance-headless")
        db.add(device)
        db.flush()
        raw, token = issue_token(user.id, device.id, DESKTOP_SCOPES)
        db.add(token)
        db.commit()

        data = {
            "serverUrl": "http://127.0.0.1:8000",
            "tokenRef": "kb-service-token",
            "deviceName": "acceptance-headless",
            "inboxFolder": "00 Inbox",
            "sourcesFolder": "10 Sources",
            "assetsFolder": "90 Assets",
            "systemFolder": "99 System",
            "autoSync": True,
            "deviceId": device.id,
            "userId": user.id,
            "tokenFallback": raw,
            "syncState": {"cursor": 0, "pending": {}, "lastRunAt": None},
        }
        plugin_dir = VAULT / ".obsidian" / "plugins" / "kb-inbox"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "data.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"user_id": user.id, "device_id": device.id}, ensure_ascii=False))


def _ensure_item(db, key: str, title: str):
    item = db.query(Item).filter(Item.id == key).one_or_none()
    if item is not None:
        src = (db.query(SourceRevision)
               .filter(SourceRevision.item_id == key)
               .order_by(SourceRevision.revision.desc()).first())
        return item, src
    uid = db.query(User).filter(User.name == "M3验收").one().id
    capture = Capture(
        user_id=uid, client_capture_id=f"accept-{key}",
        request_hash=f"hash-{key}", input_json={"kind": "text", "text": "验收样例"},
    )
    db.add(capture)
    db.flush()
    item = Item(id=key, user_id=uid, capture_id=capture.id,
                source_revision=1, bundle_revision=0, pipeline_state="queued")
    db.add(item)
    db.flush()
    src = SourceRevision(
        item_id=item.id, user_id=uid, revision=1,
        content_hash=f"ch-{key}", metadata_json={
            "platform": "acceptance", "title": title,
            "captured_at": utcnow().isoformat(), "source_locator": {},
            "coverage": "full_text", "content_scope": "text",
        }, artifacts_json={},
    )
    db.add(src)
    db.commit()
    return item, src


def cmd_publish(args) -> None:
    store = ObjectStore()
    with _session_factory()() as db:
        item, src = _ensure_item(db, args.key, args.title)
        uid = item.user_id
        rev = item.bundle_revision + 1
        normalized = (
            f"# {args.title}\n\n这是 {args.key} 的验收正文（r{rev}）。\n\n"
            "段落 s0001：核心观点一句。"
        ).encode("utf-8")
        f_norm = register_file(db, store, user_id=uid, item_id=item.id,
                               data=normalized, relative_path="normalized.md",
                               role="source_material", mime="text/markdown")
        capture_doc = {
            "capture": {"kind": "text", "text": "验收样例正文", "user_note": None},
            "received_at": utcnow().isoformat(),
        }
        f_cap = register_file(
            db, store, user_id=uid, item_id=item.id,
            data=json.dumps(capture_doc, ensure_ascii=False).encode("utf-8"),
            relative_path="capture.json", role="source_material", mime="application/json")
        preview = f"## 一句话摘要\n\n验收条目 r{rev} 预览（[[normalized#s0001|s0001]]）。"
        f_prev = register_file(db, store, user_id=uid, item_id=item.id,
                               data=preview.encode("utf-8"), relative_path="preview.md",
                               role="preview", mime="text/markdown")
        bundle = publish_bundle(
            db, store, item=item, source=src,
            files=[f_norm, f_cap, f_prev],
            processing_state="ready", pipeline_state="ready",
            result_file_id=f_prev.file_id,
        )
        db.commit()
        print(json.dumps({"key": args.key, "revision": bundle.revision}))


def cmd_evil_manifest(args) -> None:
    """篡改最新 bundle 的 manifest 并更新登记摘要——模拟被污染的投递清单（A16）。"""
    store = ObjectStore()
    with _session_factory()() as db:
        bundle = (db.query(BundleRevision)
                  .filter(BundleRevision.item_id == args.key)
                  .order_by(BundleRevision.revision.desc()).first())
        if bundle is None:
            raise SystemExit(f"条目 {args.key} 尚无 bundle")
        manifest = json.loads(store.read_object(bundle.manifest_key))
        if args.evil_path:
            manifest["files"][0]["relative_path"] = args.evil_path
        if args.evil_hash:
            manifest["files"][0]["sha256"] = "0" * 64
        sha, key, _ = store.put_bytes(canonical_json(manifest))
        bundle.manifest_key = key
        bundle.manifest_sha256 = sha
        db.commit()
        print(json.dumps({"key": args.key, "revision": bundle.revision,
                          "evil_path": args.evil_path, "evil_hash": args.evil_hash}))


def cmd_tombstone(args) -> None:
    with _session_factory()() as db:
        item = db.query(Item).filter(Item.id == args.key).one_or_none()
        if item is None:
            raise SystemExit(f"条目 {args.key} 不存在")
        item.deleted_at = utcnow()
        db.commit()
        print(json.dumps({"key": args.key, "deleted_at": item.deleted_at.isoformat()}))


def cmd_receipts(_args) -> None:
    with _session_factory()() as db:
        rows = db.query(Receipt).all()
        out = [{"item_id": r.item_id, "revision": r.bundle_revision,
                "device": r.device_id[:8], "commit": r.local_commit_id} for r in rows]
        print(json.dumps(out, ensure_ascii=False, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup").set_defaults(func=cmd_setup)

    p = sub.add_parser("publish")
    p.add_argument("key")
    p.add_argument("--title", default="M3 验收条目")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("evil-manifest")
    p.add_argument("key")
    p.add_argument("--evil-path", default=None)
    p.add_argument("--evil-hash", action="store_true")
    p.set_defaults(func=cmd_evil_manifest)

    p = sub.add_parser("tombstone")
    p.add_argument("key")
    p.set_defaults(func=cmd_tombstone)

    sub.add_parser("receipts").set_defaults(func=cmd_receipts)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

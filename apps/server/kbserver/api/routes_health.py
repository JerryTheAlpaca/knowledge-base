"""健康检查（docs/02 §10.1）：只返回布尔状态，不暴露内部路径或配置。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..storage.objects import ObjectStore

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict:
    return {"live": True}


@router.get("/ready")
def ready(response: Response, db: Session = Depends(get_db)) -> dict:
    ok_db = False
    ok_store = False
    ok_key = False
    try:
        db.execute(text("SELECT 1"))
        ok_db = True
    except Exception:
        ok_db = False
    try:
        store = ObjectStore()
        probe_key = ".healthcheck"
        store.put_bytes(b"ok")
        ok_store = store.object_exists(store.storage_key(__import__("hashlib").sha256(b"ok").hexdigest()))
    except Exception:
        ok_store = False
    try:
        settings = get_settings()
        key = settings.load_master_key()
        ok_key = len(key) == 32
    except Exception:
        ok_key = False
    ready_all = ok_db and ok_store and ok_key
    response.status_code = 200 if ready_all else 503
    return {"ready": ready_all, "database": ok_db, "storage": ok_store, "master_key": ok_key}

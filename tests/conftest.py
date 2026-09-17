"""测试环境：在任何 kbserver 导入之前配置隔离环境变量。"""
from __future__ import annotations

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / "apps" / "server"
sys.path.insert(0, str(SERVER_DIR))

_TMP = Path(tempfile.mkdtemp(prefix="kbinbox-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["OBJECTS_DIR"] = str(_TMP / "objects")
os.environ["TMP_DIR"] = str(_TMP / "tmp")
os.environ["DELETIONS_DIR"] = str(_TMP / "deletions")

# master key 以 base64 文本写入：直接写随机原始字节时，若首尾恰好是空白字节
# （0x20/\t/\n/\r，概率约 4.7%），会被 config.load_master_key 的 strip() 剥离导致解码失败。
os.environ["MASTER_KEY_FILE"] = str(_TMP / "master.key")
(_TMP / "master.key").write_text(
    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"), encoding="ascii"
)
os.environ["MASTER_KEY_VERSION"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from kbserver.app import create_app  # noqa: E402
from kbserver.db import make_engine, make_session_factory  # noqa: E402
from kbserver.models import Base, Device, User  # noqa: E402
from kbserver.security.tokens import DESKTOP_SCOPES, PHONE_SCOPES, issue_token  # noqa: E402

from datetime import timedelta  # noqa: E402

from kbserver.models import utcnow  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    settings_obj = None
    from kbserver.config import get_settings
    settings_obj = get_settings()
    settings_obj.ensure_dirs()
    eng = make_engine(settings_obj)
    Base.metadata.create_all(eng)
    yield eng


@pytest.fixture(scope="session")
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture(scope="session")
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db(session_factory):
    db = session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def make_user_with_tokens(db, name: str):
    """创建用户 + 手机/桌面设备与 Token，返回 dict。"""
    user = User(name=name)
    db.add(user)
    db.flush()
    out = {"user_id": user.id}
    for kind, scopes in (("phone", PHONE_SCOPES), ("desktop", DESKTOP_SCOPES)):
        device = Device(user_id=user.id, kind=kind, name=f"{name}-{kind}")
        db.add(device)
        db.flush()
        raw, token = issue_token(user.id, device.id, scopes)
        db.add(token)
        out[kind] = {"device_id": device.id, "token": raw}
    db.commit()
    return out


@pytest.fixture()
def user_a(db):
    return make_user_with_tokens(db, "用户A")


@pytest.fixture()
def user_b(db):
    return make_user_with_tokens(db, "用户B")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

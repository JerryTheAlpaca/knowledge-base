"""管理员 CLI（docs/02 §4.5、§9.1）：

    python -m kbserver.cli create-user --name 张三
    python -m kbserver.cli pairing-code --user <user_id> --kind desktop --device-name 我的电脑
    python -m kbserver.cli list-users

只生成用户与一次性配对码，不要求管理员代填模型 Key。
"""
from __future__ import annotations

import argparse
import uuid

from .config import get_settings
from .db import make_engine, make_session_factory
from .models import Base, User
from .security.tokens import DESKTOP_SCOPES, PHONE_SCOPES, WEB_SCOPES, issue_pairing_code

SCOPES_BY_KIND = {"desktop": DESKTOP_SCOPES, "phone": PHONE_SCOPES, "web": WEB_SCOPES}


def _prepare():
    settings = get_settings()
    settings.ensure_dirs()
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def cmd_create_user(args) -> None:
    sf = _prepare()
    with sf() as db:
        user = User(name=args.name)
        db.add(user)
        db.commit()
        print(f"user_id: {user.id}")
        print(f"name:    {user.name}")


def cmd_pairing_code(args) -> None:
    sf = _prepare()
    with sf() as db:
        user = db.get(User, args.user)
        if user is None:
            # 允许用名称匹配唯一用户
            matches = db.query(User).filter(User.name == args.user).all()
            if len(matches) == 1:
                user = matches[0]
            else:
                raise SystemExit(f"用户不存在：{args.user}")
        kind = args.kind
        if kind not in SCOPES_BY_KIND:
            raise SystemExit(f"设备类型必须是 {sorted(SCOPES_BY_KIND)}")
        raw, code = issue_pairing_code(user.id, kind, SCOPES_BY_KIND[kind])
        db.add(code)
        db.commit()
        print(f"配对码（10 分钟内有效，仅可使用一次，只显示这一次）：")
        print(raw)


def cmd_list_users(args) -> None:
    sf = _prepare()
    with sf() as db:
        for u in db.query(User).order_by(User.created_at).all():
            print(f"{u.id}  {u.name}  status={u.status}  created={u.created_at.isoformat()}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="kbserver", description="Knowledge Inbox 管理命令")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="创建用户")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("pairing-code", help="生成一次性配对码")
    p.add_argument("--user", required=True, help="user_id 或用户名")
    p.add_argument("--kind", default="desktop", choices=["desktop", "phone", "web"])
    p.set_defaults(func=cmd_pairing_code)

    p = sub.add_parser("list-users", help="列出用户")
    p.set_defaults(func=cmd_list_users)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

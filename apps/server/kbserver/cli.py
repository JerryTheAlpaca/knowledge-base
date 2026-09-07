"""管理员 CLI（docs/02 §4.5、§9.1）：

    python -m kbserver.cli create-user --name 张三
    python -m kbserver.cli pairing-code --user <user_id> --kind desktop --device-name 我的电脑
    python -m kbserver.cli list-users
    python -m kbserver.cli reconcile [--resolve billed|refunded] [--min-age-hours 1]

只生成用户与一次性配对码，不要求管理员代填模型 Key。
"""
from __future__ import annotations

import argparse
import uuid

from . import reconcile
from .config import get_settings
from .db import make_engine, make_session_factory
from .models import Base, User, utcnow
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


def cmd_reconcile(args) -> None:
    """对账滞留的供应商操作：reserved 直接退款；sent 需人工核实后落账。"""
    sf = _prepare()
    with sf() as db:
        stuck = reconcile.stuck_operations(db, min_age_hours=args.min_age_hours)
        if not stuck:
            print("没有待对账的供应商操作。")
            return
        now = args._now
        print(f"待对账操作 {len(stuck)} 条：")
        for s in stuck:
            age_h = (now - s["created_at"]).total_seconds() / 3600 if s["created_at"] else -1
            cost = s["reserved_cost"] / 1_000_000
            print(
                f"  op={s['operation_id']}  user={s['user_id']}  state={s['state']}"
                f"  job={s['job_state'] or '无'}{'  (条目已删除)' if s['item_deleted'] else ''}"
                f"  预留={cost:.4f} {s['currency']}  年龄={age_h:.1f}h"
            )
        if args.resolve == "report":
            print("\n仅列出（默认）。核对供应商账单后执行：")
            print("  --resolve refunded  # 未计费 → 全额退款")
            print("  --resolve billed    # 已计费 → 按预留结算为已提交费用")
            print("注意：sent 才可能 billed；reserved 一律按 refunded 处理。请逐条核实后执行。")
            return
        changed = 0
        for s in stuck:
            op = s["operation"]
            outcome = args.resolve
            if op.state == "reserved":
                outcome = "refunded"  # 请求未发出，永远不按已计费结算
            reconcile.resolve_operation(db, op, outcome)
            print(f"已对账 {op.id} → {outcome}")
            changed += 1
        db.commit()
        print(f"完成：{changed} 条已落账。")


def _utcnow():
    from .models import utcnow as _u
    return _u()


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

    p = sub.add_parser("reconcile", help="对账滞留的供应商操作（sent/reserved）")
    p.add_argument("--resolve", default="report", choices=["report", "billed", "refunded"],
                   help="report=仅列出；billed=确认已计费；refunded=未计费退款")
    p.add_argument("--min-age-hours", type=float, default=1.0,
                   help="只列出创建时间早于该小时数的操作（默认 1）")
    p.set_defaults(func=cmd_reconcile)

    args = parser.parse_args()
    args._now = utcnow()
    args.func(args)


if __name__ == "__main__":
    main()

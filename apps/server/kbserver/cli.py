"""管理 CLI（docs/02 §4.5、docs/05 §4.3、§5.3）：

    python -m kbserver.cli create-user --name 张三
    python -m kbserver.cli list-users
    python -m kbserver.cli bind-auth-subject --user <kb_user_id> --auth-user <中心user.id> [--dry-run]
    python -m kbserver.cli recover-waiting-budget
    python -m kbserver.cli reconcile [--resolve unknown_outcome|failed] [--min-age-hours 1]
    python -m kbserver.cli reextract [--user <kb_user_id>] [--limit N] [--dry-run]

统一登录上线后不再签发配对码；设备授权走浏览器流程（docs/05 §4.5）。
"""
from __future__ import annotations

import argparse

from . import reconcile
from .config import get_settings
from .db import make_engine, make_session_factory
from .models import Base, Item, ProviderOperation, SourceRevision, User, utcnow


def _prepare():
    settings = get_settings()
    settings.ensure_dirs()
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def cmd_create_user(args) -> None:
    """本地兜底建用户（正常路径是中心注册 + SSO 首登自动映射）。"""
    sf = _prepare()
    with sf() as db:
        user = User(name=args.name)
        db.add(user)
        db.commit()
        print(f"user_id: {user.id}")
        print(f"name:    {user.name}")


def cmd_list_users(args) -> None:
    sf = _prepare()
    with sf() as db:
        for u in db.query(User).order_by(User.created_at).all():
            subject = u.auth_subject or "（未绑定中心账号）"
            print(f"{u.id}  {u.name}  status={u.status}  auth_subject={subject}  created={u.created_at.isoformat()}")


def cmd_bind_auth_subject(args) -> None:
    """把既有 KB 用户绑定到中心账号（docs/05 §4.3 映射清单的人工确认步骤）。

    只增加身份映射，保留原 user_id；重复运行无副作用，冲突不覆盖。
    """
    sf = _prepare()
    with sf() as db:
        user = db.get(User, args.user)
        if user is None:
            matches = db.query(User).filter(User.name == args.user).all()
            if len(matches) == 1:
                user = matches[0]
            else:
                raise SystemExit(f"用户不存在或有重名：{args.user}")
        existing = db.query(User).filter(User.auth_subject == args.auth_user).one_or_none()
        if existing is not None and existing.id != user.id:
            raise SystemExit(
                f"中心账号 {args.auth_user} 已绑定到本地用户 {existing.id}（{existing.name}）；"
                "一对一映射冲突，不覆盖。"
            )
        if user.auth_subject == args.auth_user:
            print("已绑定，无需变更。")
            return
        if user.auth_subject is not None and not args.force:
            raise SystemExit(
                f"该本地用户已绑定 {user.auth_subject}；确认更换请加 --force。"
            )
        if args.dry_run:
            print(f"[dry-run] 将绑定：KB {user.id}（{user.name}） <-> 中心 {args.auth_user}")
            return
        user.auth_subject = args.auth_user
        db.commit()
        print(f"已绑定：KB {user.id}（{user.name}） <-> 中心 {args.auth_user}")
        print("注意：该用户的所有条目、凭据、设备保持原 user_id 不变。")


def cmd_recover_waiting_budget(args) -> None:
    """旧 waiting_budget 条目恢复（docs/05 §5.3）：一次性、可重复执行。

    排除已删除、有在途 sent/unknown_outcome 操作的条目；其余转 queued 入队
    enrich——enrich 预备阶段会按当前凭据/材料自行落到正确状态（waiting_key /
    needs_input / 正常加工），不重复创建任务（jobs 唯一约束幂等复位）。
    """
    sf = _prepare()
    from .models import Job

    with sf() as db:
        items = (
            db.query(Item)
            .filter(Item.pipeline_state == "waiting_budget", Item.deleted_at.is_(None))
            .all()
        )
        requeued = skipped = 0
        from .domain import pipeline

        for it in items:
            # 排除有在途/未知操作的条目（docs/05 §5.3）：按最新操作归属该条目的任务判断
            latest_op = (
                db.query(ProviderOperation)
                .filter(ProviderOperation.user_id == it.user_id)
                .order_by(ProviderOperation.created_at.desc())
                .first()
            )
            has_pending = False
            if latest_op is not None and latest_op.job_id and latest_op.state in ("sent", "unknown_outcome"):
                job = db.get(Job, latest_op.job_id)
                if job is not None and job.item_id == it.id:
                    has_pending = True
            if has_pending:
                skipped += 1
                print(f"  跳过（有在途/未知操作）：{it.id}")
                continue
            pipeline.enqueue_stage(
                db, user_id=it.user_id, item_id=it.id, source_revision=it.source_revision,
                stage="enrich", reset_attempt=True,
            )
            it.pipeline_state = "queued"
            it.state_detail = "预算机制已下线，恢复排队加工"
            requeued += 1
        db.commit()
        print(f"完成：恢复 {requeued} 条，跳过 {skipped} 条。")
        if requeued == 0 and skipped == 0:
            print("没有等待预算的条目。")


def cmd_reextract(args) -> None:
    """批量重新提取来源（只为拿到新的派生材料，例如阅读层段落）。

    等价于逐条点「重新提取来源」：重新跑提取器，旧来源版本保留；提取结果
    无变化则不新增版本，有变化会重新入队加工（会用模型额度）。
    只处理未删除、有来源 URL、且当前不在提取/加工中的条目；`--dry-run` 只列出。
    """
    sf = _prepare()
    from .domain import pipeline

    busy_states = ("queued", "extracting", "enriching")
    with sf() as db:
        query = db.query(Item).filter(Item.deleted_at.is_(None))
        if args.user:
            query = query.filter(Item.user_id == args.user)
        items = query.order_by(Item.created_at).all()
        planned = []
        for it in items:
            src = (
                db.query(SourceRevision)
                .filter(SourceRevision.item_id == it.id, SourceRevision.revision == it.source_revision)
                .one_or_none()
            )
            if src is None or not (src.metadata_json or {}).get("original_url"):
                continue  # 没有可重新提取的来源 URL（纯文字/上传/已转写条目）
            if it.pipeline_state in busy_states:
                continue
            planned.append((it, src))
        if args.limit:
            planned = planned[: args.limit]
        print(f"待重新提取 {len(planned)} 条（共扫描 {len(items)} 条）。")
        for it, src in planned:
            print(f"  {it.id}  rev={it.source_revision}  state={it.pipeline_state}"
                  f"  {(src.metadata_json or {}).get('platform') or '?'}")
        if args.dry_run:
            print("仅列出，未入队。去掉 --dry-run 执行。")
            return
        for it, _src in planned:
            pipeline.enqueue_stage(
                db, user_id=it.user_id, item_id=it.id, source_revision=it.source_revision,
                stage="extract", reset_attempt=True,
            )
            it.pipeline_state = "queued"
            it.state_detail = "重新提取来源（补新的派生材料）"
        db.commit()
        print(f"完成：已入队 {len(planned)} 条。")


def cmd_reconcile(args) -> None:
    """处置滞留的供应商操作（不含金额语义）。"""
    sf = _prepare()
    with sf() as db:
        stuck = reconcile.stuck_operations(db, min_age_hours=args.min_age_hours)
        if not stuck:
            print("没有待处置的供应商操作。")
            return
        now = args._now
        print(f"待处置操作 {len(stuck)} 条：")
        for s in stuck:
            age_h = (now - s["created_at"]).total_seconds() / 3600 if s["created_at"] else -1
            print(
                f"  op={s['operation_id']}  user={s['user_id']}  state={s['state']}"
                f"  job={s['job_state'] or '无'}{'  (条目已删除)' if s['item_deleted'] else ''}"
                f"  条目状态={s['item_state'] or '无'}  年龄={age_h:.1f}h"
            )
        if args.resolve == "report":
            print("\n仅列出（默认）。处置方式：")
            print("  --resolve unknown_outcome  # sent → 结果未知，条目可显式重新加工")
            print("  --resolve failed           # prepared → 未发出即中断，可安全重新规划")
            print("注意：不自动记成功、不自动重发。")
            return
        changed = 0
        for s in stuck:
            op = s["operation"]
            try:
                reconcile.resolve_operation(db, op, args.resolve)
            except ValueError as exc:
                print(f"  跳过 {op.id}：{exc}")
                continue
            print(f"已处置 {op.id} → {args.resolve}")
            changed += 1
        db.commit()
        print(f"完成：{changed} 条已落定。")


def main() -> None:
    parser = argparse.ArgumentParser(prog="kbserver", description="Knowledge Inbox 管理命令")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="本地兜底创建用户（正常走中心注册）")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("list-users", help="列出用户与中心账号绑定状态")
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser("bind-auth-subject", help="把既有 KB 用户绑定到中心账号（人工确认映射）")
    p.add_argument("--user", required=True, help="KB user_id 或用户名")
    p.add_argument("--auth-user", required=True, help="中心认证的不可变 user.id")
    p.add_argument("--dry-run", action="store_true", help="只显示将要做的绑定")
    p.add_argument("--force", action="store_true", help="该用户已绑定时强制更换")
    p.set_defaults(func=cmd_bind_auth_subject)

    p = sub.add_parser("recover-waiting-budget", help="恢复旧 waiting_budget 条目（可重复执行）")
    p.set_defaults(func=cmd_recover_waiting_budget)

    p = sub.add_parser("reextract", help="批量重新提取来源（补新的派生材料，如阅读层段落）")
    p.add_argument("--user", default=None, help="只处理该 KB user_id；默认全部用户")
    p.add_argument("--limit", type=int, default=None, help="最多处理多少条")
    p.add_argument("--dry-run", action="store_true", help="只列出将要重新提取的条目")
    p.set_defaults(func=cmd_reextract)

    p = sub.add_parser("reconcile", help="处置滞留的供应商操作（sent/prepared）")
    p.add_argument("--resolve", default="report", choices=["report", "unknown_outcome", "failed"],
                   help="report=仅列出；unknown_outcome=记为结果未知；failed=记为未发出中断")
    p.add_argument("--min-age-hours", type=float, default=1.0,
                   help="只列出创建时间早于该小时数的操作（默认 1）")
    p.set_defaults(func=cmd_reconcile)

    args = parser.parse_args()
    args._now = utcnow()
    args.func(args)


if __name__ == "__main__":
    main()

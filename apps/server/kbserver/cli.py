"""管理 CLI（docs/02 §4.5、docs/05 §4.3、§5.3）：

    python -m kbserver.cli create-user --name 张三
    python -m kbserver.cli list-users
    python -m kbserver.cli bind-auth-subject --user <kb_user_id> --auth-user <中心user.id> [--dry-run]
    python -m kbserver.cli recover-waiting-budget
    python -m kbserver.cli reconcile [--resolve unknown_outcome|failed] [--min-age-hours 1]
    python -m kbserver.cli reparagraph [--user <kb_user_id>] [--dry-run]
    python -m kbserver.cli remerge [--user <kb_user_id>] [--item <item_id>] [--dry-run]

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


def cmd_reparagraph(args) -> None:
    """给已有来源版本补算阅读层段落（不重新提取、不调用模型）。

    按现有 segments 计算 paragraphs，补发 `readable.md` 与带段落映射的
    `segments.json`，发布一个新的 Bundle 版本（来源版本不变）。已经算过或
    没有片段索引的条目跳过；`--dry-run` 只列出。
    """
    import json

    sf = _prepare()
    from .domain import pipeline
    from .extractors import paragraphs as parafmt
    from .repositories import core as repo
    from .storage.objects import ObjectStore

    store = ObjectStore()
    with sf() as db:
        query = db.query(Item).filter(Item.deleted_at.is_(None), Item.bundle_revision.isnot(None))
        if args.user:
            query = query.filter(Item.user_id == args.user)
        items = query.order_by(Item.created_at).all()
        done = skipped = 0
        for it in items:
            bundle = repo.get_bundle(db, it.user_id, it.id, it.bundle_revision)
            if bundle is None:
                skipped += 1
                continue
            manifest = json.loads(store.read_object(bundle.manifest_key).decode("utf-8"))
            entries = manifest.get("files") or []
            if any(f.get("relative_path") == "readable.md" for f in entries):
                skipped += 1  # 已有段落版
                continue
            seg_entry = next((f for f in entries if f.get("relative_path") == "segments.json"), None)
            seg_file = None
            if seg_entry is not None:
                seg_file = repo.get_file(db, it.user_id, seg_entry["file_id"], item_id=it.id)
            if seg_file is None or not store.object_exists(seg_file.storage_key):
                skipped += 1
                continue
            doc = json.loads(store.read_object(seg_file.storage_key).decode("utf-8"))
            segments = doc.get("segments") or []
            if not segments:
                skipped += 1
                continue
            paragraphs = parafmt.group_paragraphs(segments)
            mapping = parafmt.segment_paragraph_map(paragraphs)
            print(f"  {it.id[:8]}  rev={it.source_revision}  片段 {len(segments)} → 段落 {len(paragraphs)}")
            if args.dry_run:
                continue
            files = []
            for f in entries:
                path = f.get("relative_path")
                if path in ("readable.md", "segments.json"):
                    continue
                row = repo.get_file(db, it.user_id, f["file_id"], item_id=it.id)
                if row is not None:
                    files.append(row)
            files.append(pipeline.register_file(
                db, store, user_id=it.user_id, item_id=it.id,
                data=parafmt.paragraphs_to_readable_md(paragraphs).encode("utf-8"),
                relative_path="readable.md", role="source_material", mime="text/markdown",
            ))
            files.append(pipeline.register_file(
                db, store, user_id=it.user_id, item_id=it.id,
                data=pipeline.canonical_json({
                    "source_revision": manifest.get("source_revision", it.source_revision),
                    "segments": [dict(s, paragraph_id=mapping.get(s.get("segment_id"))) for s in segments],
                    "paragraphs": paragraphs,
                }),
                relative_path="segments.json", role="source_material", mime="application/json",
            ))
            source = (
                db.query(SourceRevision)
                .filter(SourceRevision.item_id == it.id, SourceRevision.revision == it.source_revision)
                .one()
            )
            pipeline.publish_bundle(
                db, store, item=it, source=source, files=files,
                processing_state=bundle.processing_state,
                pipeline_state=it.pipeline_state,
                warnings=manifest.get("warnings"),
                result_file_id=(manifest.get("processing") or {}).get("result_file_id"),
            )
            done += 1
        db.commit()
        if args.dry_run:
            print(f"仅列出：{len(items) - skipped} 条待补算，去掉 --dry-run 执行。")
        print(f"完成：补算 {done} 条，跳过 {skipped} 条（已有段落版或无片段索引）。")


def cmd_remerge(args) -> None:
    """用存储的识别结果按当前合并逻辑重算 ASR 条目（不重新识别音频）。

    读取 Bundle 内 asr/asr_raw.json，重跑 _merge_results 与规范化，经共享
    发布路径生成新不可变来源版本（normalized/readable/segments/transcript
    随新句段重登记，asr 原始输出与来源元数据保持不变）。句段时间与文本
    均无变化时跳过不新增版本；`--dry-run` 只列出；重发布后按用户
    「AI 自动加工」开关入 enrich（会消耗模型用量）。
    """
    import json

    sf = _prepare()
    from .domain import pipeline
    from .extractors import subtitles as subfmt
    from .models import AsrRun
    from .repositories import core as repo
    from .storage.objects import ObjectStore
    from .workers import asr as asr_mod
    from .workers.publish import publish_segments_revision

    store = ObjectStore()
    with sf() as db:
        runs = db.query(AsrRun).filter(AsrRun.state == "succeeded").order_by(AsrRun.created_at).all()
        items_with_asr = sorted({run.item_id for run in runs})
        redone = unchanged = skipped = 0
        for item_id in items_with_asr:
            it = db.get(Item, item_id)
            if it is None or it.deleted_at is not None:
                skipped += 1
                continue
            if args.user and it.user_id != args.user:
                skipped += 1
                continue
            if args.item and it.id != args.item:
                skipped += 1
                continue
            # 只重算「当前版本由 ASR 发布」的条目：run 记录的是发布前版本号
            # （发布后 item 前进一格），不能直接比对；用户补充新材料后当前
            # 版本没有 asr 元数据，自然跳过，不会覆盖新材料。
            source = (
                db.query(SourceRevision)
                .filter(SourceRevision.item_id == it.id,
                        SourceRevision.revision == it.source_revision)
                .one_or_none()
            )
            if source is None or not (source.metadata_json or {}).get("asr"):
                skipped += 1
                continue
            if (source.metadata_json or {}).get("edited_by_user") and not args.force:
                # 用户在原文编辑过当前版本：机器重算不得覆盖人工内容；
                # 确认要重算时用 --force 显式覆盖（编辑版仍在历史中）
                skipped += 1
                continue
            bundle = repo.get_bundle(db, it.user_id, it.id, it.bundle_revision) if it.bundle_revision else None
            if bundle is None:
                skipped += 1
                continue
            manifest_doc = json.loads(store.read_object(bundle.manifest_key).decode("utf-8"))
            entries = {f.get("relative_path"): f for f in (manifest_doc.get("files") or [])}

            def _load(rel):
                entry = entries.get(rel)
                row = repo.get_file(db, it.user_id, entry["file_id"], item_id=it.id) if entry else None
                if row is None or not store.object_exists(row.storage_key):
                    return None
                return json.loads(store.read_object(row.storage_key).decode("utf-8"))

            raw_doc = _load("asr/asr_raw.json")
            if not raw_doc or not raw_doc.get("chunks"):
                skipped += 1
                continue
            results = [
                {
                    "core_start": c["core_start"], "core_end": c["core_end"],
                    "input_start": c["input_start"],
                    "text": c.get("text") or "",
                    "tokens": c.get("tokens"), "timestamps": c.get("timestamps"),
                }
                for c in raw_doc["chunks"]
                if c.get("core_start") is not None
            ]
            if not results:
                skipped += 1
                continue
            records, _silence = asr_mod._merge_results({}, results)
            mf_doc = _load("asr/asr_manifest.json")
            total_s = ((mf_doc or {}).get("pcm_manifest") or {}).get("total_duration_s") or None
            segments, _w = subfmt.normalize_records(
                records, source="machine_asr", video_duration_s=total_s)
            for seg in segments:
                seg["origin"] = "asr"
                seg["confidence"] = None
            if not segments:
                skipped += 1
                continue

            cur_doc = _load("segments.json")
            cur_key = [
                (s.get("start_ms"), s.get("end_ms"), s.get("text"))
                for s in ((cur_doc or {}).get("segments") or [])
            ]
            new_key = [(s["start_ms"], s["end_ms"], s["text"]) for s in segments]
            if cur_key == new_key:
                unchanged += 1
                continue

            print(f"  {it.id[:8]}  rev={it.source_revision}  "
                  f"句段 {len(cur_key)} → {len(new_key)}")
            if args.dry_run:
                redone += 1
                continue
            extra = [pipeline.register_file(
                db, store, user_id=it.user_id, item_id=it.id,
                data=subfmt.segments_to_srt(segments).encode("utf-8"),
                relative_path="transcript.srt", role="source_material",
                mime="application/x-subrip",
            )]
            publish_segments_revision(
                db, store, None, it, source, segments=segments,
                warnings=list(manifest_doc.get("warnings") or []),
                extra_files=extra, meta_updates={},
            )
            redone += 1
        db.commit()
        if args.dry_run:
            print(f"仅列出：{redone} 条待重算，去掉 --dry-run 执行。")
        print(f"完成：重算 {redone} 条，无变化 {unchanged} 条，跳过 {skipped} 条。")


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

    p = sub.add_parser("reparagraph", help="给已有来源版本补算阅读层段落（不重新提取）")
    p.add_argument("--user", default=None, help="只处理该 KB user_id；默认全部用户")
    p.add_argument("--dry-run", action="store_true", help="只列出将要补算的条目")
    p.set_defaults(func=cmd_reparagraph)

    p = sub.add_parser("remerge", help="按当前合并逻辑重算 ASR 条目句段（不重新识别音频）")
    p.add_argument("--user", default=None, help="只处理该 KB user_id；默认全部用户")
    p.add_argument("--item", default=None, help="只处理该条目")
    p.add_argument("--force", action="store_true", help="覆盖人工编辑过的版本（默认跳过）")
    p.add_argument("--dry-run", action="store_true", help="只列出将要重算的条目")
    p.set_defaults(func=cmd_remerge)

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

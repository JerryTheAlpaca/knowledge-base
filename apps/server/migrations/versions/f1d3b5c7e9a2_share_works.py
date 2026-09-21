"""分享作品对象与模型调用归属扩展（docs/20 §11）。

新增六张表承载「选材料 → 澄清需求 → 整合 → 生成 HTML → 预览/修改 → 分享」这条
链路：作品、任务与检查点、不可变可用版本、商品引用、真实会话历史与消息。
分享任务不借用 items/jobs：Job 的唯一约束是单条目维度，StoredFile.item_id 非空，
把多篇作品伪装成某一篇会污染原有链路（docs/20 §2.1）。

provider_operations 增加可空 share_run_id + step_key，让一轮分享里的澄清、整理、
代码生成与修复分别可追踪；job_id 与 share_run_id 互斥（两者都空仍是连接测试）。
SQLite 加 CHECK 需要重建表，用 batch_alter_table 完成，数据不丢。

Revision ID: f1d3b5c7e9a2
Revises: d7f2a9c4b6e1
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f1d3b5c7e9a2'
down_revision = 'd7f2a9c4b6e1'
branch_labels = None
depends_on = None


def _ids(name: str) -> sa.Column:
    return sa.Column(name, sa.String(length=36), nullable=False)


def upgrade() -> None:
    op.create_table(
        'share_works',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False, server_default=''),
        # 三个当前指针列不建外键：与子表互相引用会让 SQLite 无法定序建表，
        # 一致性由提交时的短事务核对（归属 + 版本 + 未删除）保证。
        sa.Column('latest_ready_revision_id', sa.String(length=36), nullable=True),
        sa.Column('published_revision_id', sa.String(length=36), nullable=True),
        sa.Column('active_run_id', sa.String(length=36), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('share_token_hash', sa.String(length=64), nullable=True),
        sa.Column('share_token_ciphertext', sa.Text(), nullable=True),
        sa.Column('share_token_dek', sa.Text(), nullable=True),
        sa.Column('share_token_nonces_json', sa.JSON(), nullable=False),
        sa.Column('share_master_key_version', sa.Integer(), nullable=True),
        sa.Column('share_status', sa.String(length=20), nullable=False, server_default='private'),
        sa.Column('share_expires_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('share_token_hash', name='uq_share_work_token_hash'),
    )
    op.create_index('ix_share_works_user_id', 'share_works', ['user_id'])
    op.create_index('ix_share_works_user_status', 'share_works', ['user_id', 'share_status'])

    op.create_table(
        'share_conversations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('work_id', sa.String(length=36), nullable=False),
        sa.Column('purpose', sa.String(length=16), nullable=False),
        sa.Column('profile_id', sa.String(length=36), nullable=True),
        sa.Column('profile_version', sa.Integer(), nullable=True),
        sa.Column('api_protocol', sa.String(length=32), nullable=False,
                  server_default='openai-compatible-chat'),
        sa.Column('context_epoch', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('prefix_artifact_key', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('prefix_hash', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('last_message_seq', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['provider_profiles.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['work_id'], ['share_works.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('work_id', 'purpose', name='uq_share_conversation'),
    )
    op.create_index('ix_share_conversations_user_id', 'share_conversations', ['user_id'])
    op.create_index('ix_share_conversations_work_id', 'share_conversations', ['work_id'])

    op.create_table(
        'share_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        _ids('user_id'), _ids('work_id'),
        sa.Column('base_revision_id', sa.String(length=36), nullable=True),
        sa.Column('request_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('content_conversation_id', sa.String(length=36), nullable=True),
        sa.Column('code_conversation_id', sa.String(length=36), nullable=True),
        sa.Column('brief_version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('brief_key', sa.String(length=200), nullable=True),
        sa.Column('pending_round_key', sa.String(length=200), nullable=True),
        sa.Column('pending_round_id', sa.String(length=36), nullable=True),
        sa.Column('confirmed_brief_version', sa.Integer(), nullable=True),
        sa.Column('confirmation_kind', sa.String(length=24), nullable=True),
        sa.Column('confirmation_message_id', sa.String(length=36), nullable=True),
        sa.Column('resume_stage', sa.String(length=30), nullable=False, server_default=''),
        sa.Column('resume_checkpoint_hash', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('state', sa.String(length=30), nullable=False, server_default='queued'),
        sa.Column('stage', sa.String(length=30), nullable=False, server_default='preparing'),
        sa.Column('reason_code', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('input_manifest_key', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('input_hash', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('profile_id', sa.String(length=36), nullable=True),
        sa.Column('profile_version', sa.Integer(), nullable=True),
        sa.Column('model_config_json', sa.JSON(), nullable=False),
        sa.Column('runtime_version', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('prompt_version', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('recipe_hash', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('repair_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('not_before', sa.DateTime(), nullable=False),
        sa.Column('lease_token', sa.String(length=64), nullable=True),
        sa.Column('lease_until', sa.DateTime(), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('checkpoint_json', sa.JSON(), nullable=False),
        sa.Column('cancel_requested_at', sa.DateTime(), nullable=True),
        sa.Column('error_detail', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['code_conversation_id'], ['share_conversations.id'], ),
        sa.ForeignKeyConstraint(['content_conversation_id'], ['share_conversations.id'], ),
        sa.ForeignKeyConstraint(['profile_id'], ['provider_profiles.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['work_id'], ['share_works.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_share_runs_user_id', 'share_runs', ['user_id'])
    op.create_index('ix_share_runs_work_id', 'share_runs', ['work_id'])
    op.create_index('ix_share_runs_state_not_before', 'share_runs', ['state', 'not_before'])
    op.create_index('ix_share_runs_user_work', 'share_runs', ['user_id', 'work_id', 'created_at'])

    op.create_table(
        'share_revisions',
        sa.Column('id', sa.String(length=36), nullable=False),
        _ids('user_id'), _ids('work_id'), _ids('run_id'),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('base_revision_id', sa.String(length=36), nullable=True),
        sa.Column('confirmed_brief_key', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('input_manifest_key', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('synthesis_key', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('source_code_key', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('html_key', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('html_sha256', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('html_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('public_references_key', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('check_report_key', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('runtime_version', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['share_runs.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['work_id'], ['share_works.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('work_id', 'revision', name='uq_share_revision'),
    )
    op.create_index('ix_share_revisions_user_id', 'share_revisions', ['user_id'])
    op.create_index('ix_share_revisions_work_id', 'share_revisions', ['work_id'])
    op.create_index('ix_share_revisions_run_id', 'share_revisions', ['run_id'])

    op.create_table(
        'share_artifacts',
        sa.Column('id', sa.String(length=36), nullable=False),
        _ids('user_id'), _ids('work_id'),
        sa.Column('run_id', sa.String(length=36), nullable=True),
        sa.Column('revision_id', sa.String(length=36), nullable=True),
        sa.Column('role', sa.String(length=40), nullable=False),
        sa.Column('storage_key', sa.String(length=200), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('mime', sa.String(length=120), nullable=False, server_default='application/octet-stream'),
        sa.Column('visibility', sa.String(length=12), nullable=False, server_default='private'),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['revision_id'], ['share_revisions.id'], ),
        sa.ForeignKeyConstraint(['run_id'], ['share_runs.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['work_id'], ['share_works.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_share_artifacts_user_id', 'share_artifacts', ['user_id'])
    op.create_index('ix_share_artifacts_work', 'share_artifacts', ['user_id', 'work_id'])
    op.create_index('ix_share_artifacts_sha', 'share_artifacts', ['sha256'])

    op.create_table(
        'share_messages',
        sa.Column('id', sa.String(length=36), nullable=False),
        _ids('user_id'), _ids('conversation_id'), _ids('run_id'),
        sa.Column('context_epoch', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content_key', sa.String(length=200), nullable=False),
        sa.Column('protocol_metadata_json', sa.JSON(), nullable=False),
        sa.Column('reply_to_round_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['share_conversations.id'], ),
        sa.ForeignKeyConstraint(['run_id'], ['share_runs.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('conversation_id', 'context_epoch', 'seq', name='uq_share_message_seq'),
    )
    op.create_index('ix_share_messages_user_id', 'share_messages', ['user_id'])
    op.create_index('ix_share_messages_conversation_id', 'share_messages', ['conversation_id'])
    op.create_index('ix_share_messages_run_id', 'share_messages', ['run_id'])
    op.create_index('ix_share_messages_user_conv', 'share_messages', ['user_id', 'conversation_id'])

    # 分享任务归属与缓存诊断字段；CHECK 需要重建表，batch 模式负责搬数据与索引
    with op.batch_alter_table('provider_operations') as batch:
        batch.add_column(sa.Column('share_run_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('step_key', sa.String(length=40), nullable=False, server_default=''))
        batch.add_column(sa.Column('conversation_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('context_epoch', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('input_message_seq', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('prefix_hash', sa.String(length=64), nullable=False, server_default=''))
        batch.add_column(sa.Column('usage_json', sa.JSON(), nullable=False, server_default='{}'))
        batch.create_foreign_key('fk_provider_ops_share_run', 'share_runs',
                                 ['share_run_id'], ['id'])
        batch.create_foreign_key('fk_provider_ops_conversation', 'share_conversations',
                                 ['conversation_id'], ['id'])
        batch.create_check_constraint(
            'ck_provider_ops_single_owner',
            'not (job_id is not null and share_run_id is not null)',
        )
    op.create_index('ix_provider_operations_share_run_id', 'provider_operations', ['share_run_id'])
    op.create_index('ix_provider_operations_conversation_id', 'provider_operations', ['conversation_id'])


def downgrade() -> None:
    # 先删索引：batch 重建表时会按现存索引重发 CREATE INDEX，
    # 列还在但索引已删才安全（顺序反了会报 no such column）
    op.drop_index('ix_provider_operations_share_run_id', table_name='provider_operations')
    op.drop_index('ix_provider_operations_conversation_id', table_name='provider_operations')
    with op.batch_alter_table('provider_operations') as batch:
        batch.drop_constraint('ck_provider_ops_single_owner', type_='check')
        batch.drop_constraint('fk_provider_ops_share_run', type_='foreignkey')
        batch.drop_constraint('fk_provider_ops_conversation', type_='foreignkey')
        for name in ('share_run_id', 'step_key', 'conversation_id', 'context_epoch',
                     'input_message_seq', 'prefix_hash', 'usage_json'):
            batch.drop_column(name)
    # SQLite 的索引随表一起删除；按依赖反序丢表
    for table in ('share_messages', 'share_artifacts', 'share_revisions',
                  'share_runs', 'share_conversations', 'share_works'):
        op.drop_table(table)

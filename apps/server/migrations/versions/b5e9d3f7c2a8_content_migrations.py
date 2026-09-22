"""内容协议 v3 迁移基础设施（docs/24 §7）：jobs.input_json 与 content_migrations 表。

- jobs.input_json：固定本次模型调用的任务内 R 引用表与块输入。进程重启后按同一
  绑定恢复，不允许中途把 R1 换成另一段原文（docs/23 §4.1 规则 4）。
- content_migrations：旧 analysis.json → v3 的转换台账。唯一键
  (user_id, item_id, input_sha256, converter_version) 让同一旧输入用同一版转换器
  重复执行不再生成新 Bundle（docs/23 §8.1 第 7 步）；旧 Bundle、manifest 与历史
  回执都不修改，所以只记新版本号。

Revision ID: b5e9d3f7c2a8
Revises: e9a3c5f7b2d4
Create Date: 2026-09-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b5e9d3f7c2a8'
down_revision = 'e9a3c5f7b2d4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('input_json', sa.JSON(), nullable=False, server_default='{}'))

    op.create_table(
        'content_migrations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('item_id', sa.String(length=36), nullable=False),
        sa.Column('input_sha256', sa.String(length=64), nullable=False),
        sa.Column('converter_version', sa.String(length=40), nullable=False),
        sa.Column('new_bundle_revision', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='complete'),
        sa.Column('notes', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'item_id', 'input_sha256', 'converter_version',
                            name='uq_content_migration_input'),
    )
    op.create_index('ix_content_migrations_user_id', 'content_migrations', ['user_id'])
    op.create_index('ix_content_migrations_item_id', 'content_migrations', ['item_id'])
    op.create_index('ix_content_migrations_user_item', 'content_migrations',
                    ['user_id', 'item_id'])


def downgrade() -> None:
    op.drop_index('ix_content_migrations_user_item', table_name='content_migrations')
    op.drop_index('ix_content_migrations_item_id', table_name='content_migrations')
    op.drop_index('ix_content_migrations_user_id', table_name='content_migrations')
    op.drop_table('content_migrations')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('input_json')

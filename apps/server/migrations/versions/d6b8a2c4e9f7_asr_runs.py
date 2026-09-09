"""docs/11：本地 ASR 检查点表 asr_runs。

Revision ID: d6b8a2c4e9f7
Revises: b7e4c1a9d2f3
Create Date: 2026-09-09

- 唯一键 (user,item,source_revision,recipe_hash)：模型切换产生新 recipe/run，
  不混合两种模型的识别输出。
- manifest_json 是准备阶段原子提交的 PCM 清单摘要；提交前不作为转写输入。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd6b8a2c4e9f7'
down_revision = 'b7e4c1a9d2f3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'asr_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('item_id', sa.String(length=36), nullable=False),
        sa.Column('source_revision', sa.Integer(), nullable=False),
        sa.Column('recipe_hash', sa.String(length=64), nullable=False),
        sa.Column('model_alias', sa.String(length=40), nullable=False),
        sa.Column('model_id', sa.String(length=120), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False, server_default='queued'),
        sa.Column('pause_reason', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('next_chunk_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('chunk_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('processed_seconds', sa.Float(), nullable=False, server_default='0'),
        sa.Column('requested_by', sa.String(length=20), nullable=False, server_default='manual'),
        sa.Column('work_dir', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('manifest_json', sa.JSON(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.UniqueConstraint('user_id', 'item_id', 'source_revision', 'recipe_hash', name='uq_asr_run'),
    )
    op.create_index('ix_asr_runs_user_item', 'asr_runs', ['user_id', 'item_id'])


def downgrade() -> None:
    op.drop_index('ix_asr_runs_user_item', table_name='asr_runs')
    op.drop_table('asr_runs')

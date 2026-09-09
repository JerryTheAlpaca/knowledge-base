"""补 c8d2e4f6a9b1 遗漏：provider_operations 历史计费列改可空

Revision ID: f4a8b2c6d9e1
Revises: e7f1a9c4b2d8
Create Date: 2026-09-09

去计费后模型不再有 reserved_cost / actual_usage_json 字段（docs/05 §5.2），
迁移 c8d2e4f6a9b1 将两列"保留为历史数据"但未去除 NOT NULL 约束：存量库上
新代码 INSERT（provider_ops.create_operation，只写新模型字段）必然触发
NOT NULL constraint failed，enrich 全量失败。

修正：两列改为可空（保留历史数据，不改值），与模型不再读写的语义对齐。
新库（create_all）本来就没有这两列，本迁移对纯 Alembic 存量库生效。
SQLite 通过 batch_alter_table 重建表。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'f4a8b2c6d9e1'
down_revision = 'e7f1a9c4b2d8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('provider_operations') as batch:
        batch.alter_column('reserved_cost', existing_type=sa.Integer(), nullable=True)
        batch.alter_column('actual_usage_json', existing_type=sa.JSON(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('provider_operations') as batch:
        batch.alter_column('reserved_cost', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('actual_usage_json', existing_type=sa.JSON(), nullable=False)

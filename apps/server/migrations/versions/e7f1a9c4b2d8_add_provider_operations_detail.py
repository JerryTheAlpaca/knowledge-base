"""补 c8d2e4f6a9b1 遗漏：provider_operations 增加 detail 列

Revision ID: e7f1a9c4b2d8
Revises: c8d2e4f6a9b1
Create Date: 2026-09-08

6de318a 将 provider_operations 的语义改为 prepared→sent→succeeded/failed/
unknown_outcome（docs/05 §5.2），模型新增 detail 列记录结束原因简短说明；
迁移 c8d2e4f6a9b1 漏了该列，导致纯 Alembic 升级的存量库与模型不一致
（reconcile 等按模型 SELECT 时报 no such column: detail）。

存量行为 NULL 语义的空说明，server_default '' 与模型 default='' 对齐；
reserved_cost/actual_usage_json 为保留的历史列，不在此处理（docs/07 §3.1）。
SQLite 通过 batch_alter_table 重建表。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'e7f1a9c4b2d8'
down_revision = 'c8d2e4f6a9b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('provider_operations') as batch:
        batch.add_column(sa.Column('detail', sa.String(length=200),
                                   nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('provider_operations') as batch:
        batch.drop_column('detail')

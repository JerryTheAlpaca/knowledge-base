"""模型配置角色：整理文本（digest）与优化文本（optimize）分档配置。

Revision ID: b9d3f5a7c1e2
Revises: c2e5f7a9b1d3
Create Date: 2026-09-15

- provider_profiles.role：llm 配置的角色，digest=整理文本（NULL 视为整理，
  兼容存量配置）、optimize=优化文本（语义分段与听错词修正）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b9d3f5a7c1e2'
down_revision = 'c2e5f7a9b1d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('provider_profiles') as batch:
        batch.add_column(sa.Column('role', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('provider_profiles') as batch:
        batch.drop_column('role')

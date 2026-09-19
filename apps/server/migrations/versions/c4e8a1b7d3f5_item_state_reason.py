"""审查 C-14：items 增加 state_reason 机器码列。

界面动作（「连接该平台」还是「更新登录信息」）与登录态更新后的定向重排队，
原先靠 state_detail 里的中文关键词匹配；文案一改就静默失效。改为独立机器码，
本迁移只加列，存量行的归类由 worker 在下次状态变化时写入。

Revision ID: c4e8a1b7d3f5
Revises: a9c4e2f6b1d7
Create Date: 2026-09-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c4e8a1b7d3f5'
down_revision = 'a9c4e2f6b1d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('items') as batch:
        batch.add_column(sa.Column('state_reason', sa.String(length=32),
                                   nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('items') as batch:
        batch.drop_column('state_reason')

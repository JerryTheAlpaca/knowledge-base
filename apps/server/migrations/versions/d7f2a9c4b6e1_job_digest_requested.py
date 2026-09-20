"""jobs 增加 digest_requested：把「用户点名要整理」记在任务上。

「AI 自动整理」与「AI 语义分段与纠错」拆成两个独立开关后，enrich 任务不再等价于
「一定要整理」。但手动「开始整理」走的是同一个 jobs 行（uq_job 按
user/item/revision/stage/recipe 去重），任务内部若只读当前开关，自动整理关着时
那个按钮就变成空点。所以把意图落在行上：手动入队置真，prepare 不再看开关。

Revision ID: d7f2a9c4b6e1
Revises: f8c1b3d5a7e9
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd7f2a9c4b6e1'
down_revision = 'f8c1b3d5a7e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('digest_requested', sa.Boolean(),
                                   nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('digest_requested')

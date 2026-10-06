"""补齐 agent_tasks.updated_at：模型有这一列，建表迁移漏了。

2026-10-06 线上实测后果：worker 启动时的租约恢复会 SELECT 全列，SQLite 报
`no such column: agent_tasks.updated_at` 直接退出，容器进入崩溃重启——不是
「agent 功能没开」，是整条后台加工链停了。存量行用当前时间补齐（这张表是
新表，正常部署里此刻还没有数据）。

Revision ID: c9a5e7b1d4f8
Revises: f6b2d8c4a1e9
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c9a5e7b1d4f8'
down_revision = 'f6b2d8c4a1e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('agent_tasks') as batch:
        # SQLite 的 ADD COLUMN 只接受常量默认值（(datetime('now')) 这种会直接报
        # "Cannot add a column with non-constant default"），所以先给一个哨兵常量，
        # 再把存量行对齐到自己的 created_at。
        batch.add_column(sa.Column('updated_at', sa.DateTime(), nullable=False,
                                   server_default=sa.text("('1970-01-01 00:00:00')")))
    op.execute("UPDATE agent_tasks SET updated_at = created_at "
               "WHERE updated_at = '1970-01-01 00:00:00'")


def downgrade() -> None:
    with op.batch_alter_table('agent_tasks') as batch:
        batch.drop_column('updated_at')

"""网页下载原文计入完成：items 增加 original_download_bundle 列。

用户在收件箱点「下载原文文件」后，这条内容已经拿到手，与插件回执一样进入终态；
记录下载时的 Bundle 版本，原文随后更新时不会沿用旧的「已下载」。

Revision ID: f8c1b3d5a7e9
Revises: c4e8a1b7d3f5
Create Date: 2026-09-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f8c1b3d5a7e9'
down_revision = 'c4e8a1b7d3f5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('items') as batch:
        batch.add_column(sa.Column('original_download_bundle', sa.Integer(),
                                   nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('items') as batch:
        batch.drop_column('original_download_bundle')

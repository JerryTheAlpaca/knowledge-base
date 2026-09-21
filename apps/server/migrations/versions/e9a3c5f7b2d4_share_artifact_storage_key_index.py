"""share_artifacts.storage_key 加索引：产物回收的存活复查不再全表扫。

保留期清理每批取 100 个 key，逐个按 storage_key 到 share_artifacts、stored_files、
bundle_revisions、uploads 四张表复查还有没有别的登记（workers/share_retention.py 的
_still_referenced）。share_artifacts 原本只有 user/work/sha 三个索引，行数随作品数
线性增长，2 核 2GB 的机器上每轮清理都要在这里全表扫。索引与模型
models.ShareArtifact.__table_args__ 保持一致。

Revision ID: e9a3c5f7b2d4
Revises: f1d3b5c7e9a2
Create Date: 2026-09-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'e9a3c5f7b2d4'
down_revision = 'f1d3b5c7e9a2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_share_artifacts_storage_key', 'share_artifacts', ['storage_key'])


def downgrade() -> None:
    op.drop_index('ix_share_artifacts_storage_key', table_name='share_artifacts')

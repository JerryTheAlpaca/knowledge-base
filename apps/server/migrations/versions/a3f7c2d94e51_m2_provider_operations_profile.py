"""M2：provider_operations 增加 profile_id、job_id 改可空

Revision ID: a3f7c2d94e51
Revises: ca92d1f2a2be
Create Date: 2026-09-07

- profile_id：用量按模型配置统计（/v1/usage by_profile）。
- job_id 可空：连接测试等无任务调用也要进 provider_operations/账本。
SQLite 通过 batch_alter_table 重建表。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'a3f7c2d94e51'
down_revision = 'ca92d1f2a2be'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('provider_operations') as batch:
        batch.add_column(sa.Column('profile_id', sa.String(length=36), nullable=True))
        batch.alter_column('job_id', existing_type=sa.String(length=36), nullable=True)
        batch.create_index('ix_provider_operations_profile', ['profile_id'])
        batch.create_foreign_key(
            'fk_provider_operations_profile', 'provider_profiles',
            ['profile_id'], ['id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('provider_operations') as batch:
        batch.drop_constraint('fk_provider_operations_profile', type_='foreignkey')
        batch.drop_index('ix_provider_operations_profile')
        batch.drop_column('profile_id')
        batch.alter_column('job_id', existing_type=sa.String(length=36), nullable=False)

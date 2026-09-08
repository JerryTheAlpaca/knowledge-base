"""统一账号与去计费改造（docs/05）：auth_subject、meta_json、device_auth_requests、reserved→prepared

Revision ID: c8d2e4f6a9b1
Revises: a3f7c2d94e51
Create Date: 2026-09-08

- users.auth_subject：中心认证不可变 user.id（可空、唯一），SSO 首次登录幂等映射。
- provider_profiles.meta_json：适配器自有元数据（B 站登录态最近检测等）。
  prices_json 列保留为历史数据，新代码不再读写（docs/05 §5.3）。
- device_auth_requests：插件浏览器授权流程（docs/05 §4.5）。
- provider_operations.state：reserved 更名为 prepared（调用待发送，无资金语义）；
  reserved_cost/actual_usage_json 保留为历史数据，新代码不再读写。
- usage_ledger 表保留为历史数据，不再产生新记录（docs/05 §5.3）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = 'c8d2e4f6a9b1'
down_revision = 'a3f7c2d94e51'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('auth_subject', sa.String(length=64), nullable=True))
        batch.create_index('ix_users_auth_subject', ['auth_subject'], unique=True)

    with op.batch_alter_table('provider_profiles') as batch:
        batch.add_column(sa.Column('meta_json', sa.JSON(), nullable=False, server_default='{}'))

    # 计费状态名改执行状态名：reserved → prepared（请求确定未发出）
    op.execute("UPDATE provider_operations SET state='prepared' WHERE state='reserved'")

    op.create_table(
        'device_auth_requests',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('device_name', sa.String(length=120), nullable=False),
        sa.Column('poll_secret_hash', sa.String(length=64), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('auth_subject', sa.String(length=64), nullable=True),
        sa.Column('central_username', sa.String(length=120), nullable=True),
        sa.Column('local_user_id', sa.String(length=36), nullable=True),
        sa.Column('device_id', sa.String(length=36), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('device_auth_requests')
    with op.batch_alter_table('provider_profiles') as batch:
        batch.drop_column('meta_json')
    op.execute("UPDATE provider_operations SET state='reserved' WHERE state='prepared'")
    with op.batch_alter_table('users') as batch:
        batch.drop_index('ix_users_auth_subject')
        batch.drop_column('auth_subject')

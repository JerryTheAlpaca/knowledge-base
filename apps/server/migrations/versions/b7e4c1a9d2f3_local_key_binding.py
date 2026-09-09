"""docs/08 §8.3：线上 Key 下发到本人设备的绑定表与设备授权申请权限。

Revision ID: b7e4c1a9d2f3
Revises: f4a8b2c6d9e1
Create Date: 2026-09-09

- local_key_bindings：记录（用户、设备、线上配置）的绑定与最近下发版本；
  只存绑定关系，不存明文 Key。
- device_auth_requests.requested_scopes_json：插件在授权时申请的额外权限
  （目前仅 profiles:bind-local），批准时按此签发。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b7e4c1a9d2f3'
down_revision = 'f4a8b2c6d9e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'local_key_bindings',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('device_id', sa.String(length=36), nullable=False),
        sa.Column('profile_id', sa.String(length=36), nullable=False),
        sa.Column('profile_version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('credential_version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_bound_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('blocked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['device_id'], ['devices.id']),
        sa.ForeignKeyConstraint(['profile_id'], ['provider_profiles.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'device_id', 'profile_id', name='uq_local_binding'),
    )
    with op.batch_alter_table('local_key_bindings') as batch:
        batch.create_index('ix_local_key_bindings_user_id', ['user_id'])
        batch.create_index('ix_local_key_bindings_device_id', ['device_id'])
        batch.create_index('ix_local_key_bindings_profile_id', ['profile_id'])

    with op.batch_alter_table('device_auth_requests') as batch:
        batch.add_column(
            sa.Column('requested_scopes_json', sa.JSON(), nullable=False, server_default='[]')
        )


def downgrade() -> None:
    with op.batch_alter_table('device_auth_requests') as batch:
        batch.drop_column('requested_scopes_json')
    op.drop_table('local_key_bindings')

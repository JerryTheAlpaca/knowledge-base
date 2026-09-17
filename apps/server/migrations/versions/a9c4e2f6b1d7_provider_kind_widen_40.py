"""docs/18 §7.2：provider_profiles.kind 扩到 40，容纳平台登录态会话类型。

wechat_channels_session（23 字符）超过原 20 字符限制；不依赖 SQLite 不强制
长度的偶然行为，显式迁移列宽并同步模型声明。

Revision ID: a9c4e2f6b1d7
Revises: b9d3f5a7c1e2
Create Date: 2026-09-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a9c4e2f6b1d7'
down_revision = 'b9d3f5a7c1e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('provider_profiles') as batch:
        batch.alter_column('kind', existing_type=sa.String(length=20),
                           type_=sa.String(length=40))


def downgrade() -> None:
    # 收缩前须确认没有超过 20 字符的 kind（如 wechat_channels_session），
    # 否则超长值会被截断；正式环境回滚前先清理相关平台会话配置。
    with op.batch_alter_table('provider_profiles') as batch:
        batch.alter_column('kind', existing_type=sa.String(length=40),
                           type_=sa.String(length=20))

"""Agent LLM 预算表（docs/27 §预算不串用）。

每个用户每个模型配置一份按日计数：入口先查（超限 429 且不转发，不产生供应商费用），
出口用 normalize_usage 归一后累加。归属三元组 (user_id, profile_id, period) 只从
签名会话 token 取，永不从请求体取。

Revision ID: d3f8a6c1b9e4
Revises: a1c7e4f9b2d6
Create Date: 2026-10-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd3f8a6c1b9e4'
down_revision = 'a1c7e4f9b2d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'agent_budgets',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('profile_id', sa.String(length=36), nullable=False),
        sa.Column('period', sa.String(length=10), nullable=False),
        sa.Column('input_tokens_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('output_tokens_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('requests_used', sa.Integer(), nullable=False, server_default='0'),
        # 签名 token 无法主动撤销：靠 TTL、这个标记与「停用 agent」三重收口
        sa.Column('exhausted_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['provider_profiles.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'profile_id', 'period', name='uq_agent_budget'),
    )
    op.create_index('ix_agent_budgets_user_id', 'agent_budgets', ['user_id'])
    op.create_index('ix_agent_budgets_profile_id', 'agent_budgets', ['profile_id'])


def downgrade() -> None:
    op.drop_index('ix_agent_budgets_profile_id', table_name='agent_budgets')
    op.drop_index('ix_agent_budgets_user_id', table_name='agent_budgets')
    op.drop_table('agent_budgets')

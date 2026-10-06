"""Agent 任务句柄表（docs/27 §异步任务表）。

MCP 工具调用有 60 秒上限，要跑模型的活只能「提交 + 轮询」。不复用 jobs 表：
jobs 按 (user,item,revision,stage,recipe) 唯一且会被重复入队复用同一行，用户同时
点「重新加工」就会把 agent 手里那个句柄的状态重置掉——它不再代表「我提交的那一次」。
执行体仍然是那条既有 Job（租约、恢复、ProviderOperation 语义都不重造）。

Revision ID: a1c7e4f9b2d6
Revises: b5e9d3f7c2a8
Create Date: 2026-10-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a1c7e4f9b2d6'
down_revision = 'b5e9d3f7c2a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'agent_tasks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('site', sa.String(length=16), nullable=False, server_default='kb'),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('item_id', sa.String(length=36), nullable=True),
        sa.Column('job_id', sa.String(length=36), nullable=True),
        sa.Column('params_json', sa.JSON(), nullable=False, server_default='{}'),
        # 允许为空：没给幂等键的提交不参与唯一约束（SQLite 里多个 NULL 互不冲突）
        sa.Column('idempotency_key', sa.String(length=64), nullable=True),
        sa.Column('state', sa.String(length=24), nullable=False, server_default='queued'),
        sa.Column('result_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('last_error', sa.Text(), nullable=False, server_default=''),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('not_before', sa.DateTime(), nullable=False),
        sa.Column('lease_token', sa.String(length=64), nullable=True),
        sa.Column('lease_until', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'site', 'idempotency_key', name='uq_agent_task_idem'),
    )
    op.create_index('ix_agent_tasks_user_id', 'agent_tasks', ['user_id'])
    op.create_index('ix_agent_tasks_item_id', 'agent_tasks', ['item_id'])
    op.create_index('ix_agent_tasks_job_id', 'agent_tasks', ['job_id'])
    op.create_index('ix_agent_tasks_state_not_before', 'agent_tasks', ['state', 'not_before'])
    op.create_index('ix_agent_tasks_user_site', 'agent_tasks', ['user_id', 'site', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_agent_tasks_user_site', table_name='agent_tasks')
    op.drop_index('ix_agent_tasks_state_not_before', table_name='agent_tasks')
    op.drop_index('ix_agent_tasks_job_id', table_name='agent_tasks')
    op.drop_index('ix_agent_tasks_item_id', table_name='agent_tasks')
    op.drop_index('ix_agent_tasks_user_id', table_name='agent_tasks')
    op.drop_table('agent_tasks')

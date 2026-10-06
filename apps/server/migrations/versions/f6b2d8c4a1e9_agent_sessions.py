"""Agent 会话与事件镜像表（docs/27 §对话记录）。

双写：agent 容器里的 `$DSH_HOME` 可以丢，A 机 SQLite 才是权威记录。
- seq 由编排服务单调分配，A 机只按 (session_id, seq) upsert，断连重发是幂等的；
- 只镜像用户可见事件，流式 delta 在容器侧合并成整条 assistant_message；
- site 是结构性维度：会话 ID 唯一约束带 site，不同站点即使 user_id 相同也互不可见。

不复用 share_conversations / share_messages：那两张按 (work_id, purpose) 组织、
正文在 ObjectStore、且绑着 HTML 分享那台状态机；agent 会话没有「作品」概念。

Revision ID: f6b2d8c4a1e9
Revises: d3f8a6c1b9e4
Create Date: 2026-10-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f6b2d8c4a1e9'
down_revision = 'd3f8a6c1b9e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'agent_sessions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('site', sa.String(length=16), nullable=False, server_default='kb'),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('last_seq', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('state', sa.String(length=20), nullable=False, server_default='open'),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site', 'session_id', name='uq_agent_session_site'),
    )
    op.create_index('ix_agent_sessions_user_id', 'agent_sessions', ['user_id'])
    op.create_index('ix_agent_sessions_user_site', 'agent_sessions',
                    ['user_id', 'site', 'updated_at'])

    op.create_table(
        'agent_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('site', sa.String(length=16), nullable=False, server_default='kb'),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('payload_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id', 'seq', name='uq_agent_event_seq'),
    )
    op.create_index('ix_agent_events_user_id', 'agent_events', ['user_id'])
    op.create_index('ix_agent_events_session_id', 'agent_events', ['session_id'])
    op.create_index('ix_agent_events_user_site', 'agent_events',
                    ['user_id', 'site', 'session_id'])


def downgrade() -> None:
    op.drop_index('ix_agent_events_user_site', table_name='agent_events')
    op.drop_index('ix_agent_events_session_id', table_name='agent_events')
    op.drop_index('ix_agent_events_user_id', table_name='agent_events')
    op.drop_table('agent_events')
    op.drop_index('ix_agent_sessions_user_site', table_name='agent_sessions')
    op.drop_index('ix_agent_sessions_user_id', table_name='agent_sessions')
    op.drop_table('agent_sessions')

"""docs/13：多来源音频接入——上传续传会话、音频原件引用、ASR 输入冻结。

Revision ID: c2e5f7a9b1d3
Revises: d6b8a2c4e9f7
Create Date: 2026-09-10

- audio_upload_sessions：音频大文件分块续传（staging 临时输入，完成后对象化）。
- audio_assets：用户上传原件引用，ASR 结束后仍受保护（docs/13 §6.3）。
- asr_runs.input_kind/input_json/input_fingerprint：冻结本次输入来源定位或
  对象引用；旧 run 缺字段时回退从 SourceRevision/locator 读取。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c2e5f7a9b1d3'
down_revision = 'd6b8a2c4e9f7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'audio_upload_sessions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('mime', sa.String(length=120), nullable=False, server_default='application/octet-stream'),
        sa.Column('total_bytes', sa.Integer(), nullable=False),
        sa.Column('offset', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('chunk_size', sa.Integer(), nullable=False, server_default='16777216'),
        sa.Column('staging_path', sa.String(length=300), nullable=False),
        sa.Column('digest_state', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('state', sa.String(length=20), nullable=False, server_default='receiving'),
        sa.Column('upload_id', sa.String(length=36), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
    )
    op.create_index('ix_audio_upload_sessions_user', 'audio_upload_sessions', ['user_id'])
    op.create_index('ix_audio_upload_sessions_state', 'audio_upload_sessions', ['state', 'expires_at'])

    op.create_table(
        'audio_assets',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('item_id', sa.String(length=36), nullable=False),
        sa.Column('source_revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('upload_id', sa.String(length=36), nullable=False),
        sa.Column('stored_file_id', sa.String(length=64), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('filename', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('mime', sa.String(length=120), nullable=False, server_default='application/octet-stream'),
        sa.Column('role', sa.String(length=40), nullable=False, server_default='original_audio'),
        sa.Column('retention_state', sa.String(length=20), nullable=False, server_default='retained'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.ForeignKeyConstraint(['upload_id'], ['uploads.id']),
    )
    op.create_index('ix_audio_assets_item_id', 'audio_assets', ['item_id'])
    op.create_index('ix_audio_assets_upload_id', 'audio_assets', ['upload_id'])
    op.create_index('ix_audio_assets_user_id', 'audio_assets', ['user_id'])

    with op.batch_alter_table('asr_runs') as batch:
        batch.add_column(sa.Column('input_kind', sa.String(length=20), nullable=False,
                                   server_default='remote'))
        batch.add_column(sa.Column('input_json', sa.JSON(), nullable=False, server_default='{}'))
        batch.add_column(sa.Column('input_fingerprint', sa.String(length=120), nullable=False,
                                   server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('asr_runs') as batch:
        batch.drop_column('input_fingerprint')
        batch.drop_column('input_json')
        batch.drop_column('input_kind')
    op.drop_index('ix_audio_assets_user_id', table_name='audio_assets')
    op.drop_index('ix_audio_assets_upload_id', table_name='audio_assets')
    op.drop_index('ix_audio_assets_item_id', table_name='audio_assets')
    op.drop_table('audio_assets')
    op.drop_index('ix_audio_upload_sessions_user_id', table_name='audio_upload_sessions')
    op.drop_table('audio_upload_sessions')

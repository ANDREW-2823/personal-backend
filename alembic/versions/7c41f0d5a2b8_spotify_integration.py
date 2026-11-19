"""spotify integration

Revision ID: 7c41f0d5a2b8
Revises: ed46598ce62e
Create Date: 2026-10-28 12:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '7c41f0d5a2b8'
down_revision: Union[str, None] = 'ed46598ce62e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'spotify_oauth_states',
        sa.Column('state', sa.String(length=64), nullable=False),
        sa.Column('code_verifier', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('state', name=op.f('pk_spotify_oauth_states')),
    )
    op.create_table(
        'spotify_tokens',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('access_token', sa.Text(), nullable=False),
        sa.Column('refresh_token', sa.Text(), nullable=False),
        sa.Column('token_type', sa.String(length=20), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('display_name', sa.String(length=120), nullable=True),
        sa.Column('account_id', sa.String(length=120), nullable=True),
        sa.Column('product', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_spotify_tokens')),
    )


def downgrade() -> None:
    op.drop_table('spotify_tokens')
    op.drop_table('spotify_oauth_states')

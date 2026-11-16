"""persist encrypted pending YouTube channel choices

Revision ID: 3d8a6f1c9b20
Revises: b7c4e1a92d60
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3d8a6f1c9b20"
down_revision: str | Sequence[str] | None = "b7c4e1a92d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("youtube_oauth_attempts", sa.Column("pending_channels", sa.JSON(), nullable=True))
    op.add_column(
        "youtube_oauth_attempts",
        sa.Column("pending_encrypted_refresh_token", sa.Text(), nullable=True),
    )
    op.add_column(
        "youtube_oauth_attempts",
        sa.Column("pending_key_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "youtube_oauth_attempts",
        sa.Column("pending_granted_scopes", sa.Text(), nullable=True),
    )
    op.add_column(
        "youtube_oauth_attempts",
        sa.Column("pending_selection_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "youtube_oauth_attempts",
        sa.Column("selection_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_youtube_oauth_attempts_pending_token_key_pair",
        "youtube_oauth_attempts",
        "(pending_encrypted_refresh_token IS NULL AND pending_key_version IS NULL) OR "
        "(pending_encrypted_refresh_token IS NOT NULL AND pending_key_version IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_youtube_oauth_attempts_pending_token_key_pair",
        "youtube_oauth_attempts",
        type_="check",
    )
    op.drop_column("youtube_oauth_attempts", "selection_completed_at")
    op.drop_column("youtube_oauth_attempts", "pending_selection_expires_at")
    op.drop_column("youtube_oauth_attempts", "pending_granted_scopes")
    op.drop_column("youtube_oauth_attempts", "pending_key_version")
    op.drop_column("youtube_oauth_attempts", "pending_encrypted_refresh_token")
    op.drop_column("youtube_oauth_attempts", "pending_channels")

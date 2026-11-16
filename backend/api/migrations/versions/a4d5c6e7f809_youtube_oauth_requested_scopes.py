"""Persist the scopes requested by each YouTube OAuth attempt.

Revision ID: a4d5c6e7f809
Revises: e3a9f17c2b6d
"""

import sqlalchemy as sa
from alembic import op

revision = "a4d5c6e7f809"
down_revision = "e3a9f17c2b6d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "youtube_oauth_attempts",
        sa.Column(
            "requested_scopes",
            sa.Text(),
            server_default=sa.text(
                "'https://www.googleapis.com/auth/youtube.readonly'"
            ),
            nullable=False,
        ),
    )
    op.add_column(
        "youtube_oauth_attempts",
        sa.Column("target_channel_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_youtube_oauth_attempts_target_channel_id_youtube_channels",
        "youtube_oauth_attempts",
        "youtube_channels",
        ["target_channel_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_youtube_oauth_attempts_target_channel_id_youtube_channels",
        "youtube_oauth_attempts",
        type_="foreignkey",
    )
    op.drop_column("youtube_oauth_attempts", "target_channel_id")
    op.drop_column("youtube_oauth_attempts", "requested_scopes")

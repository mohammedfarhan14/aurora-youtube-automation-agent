"""Add YouTube OAuth attempts and connected channels.

Revision ID: c9d3a71e4b20
Revises: 62eb1a5ff3ab
"""

import sqlalchemy as sa
from alembic import op

revision = "c9d3a71e4b20"
down_revision = "62eb1a5ff3ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "youtube_oauth_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "length(state_hash) = 64",
            name=op.f("ck_youtube_oauth_attempts_state_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_youtube_oauth_attempts_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_youtube_oauth_attempts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_youtube_oauth_attempts")),
        sa.UniqueConstraint(
            "state_hash", name=op.f("uq_youtube_oauth_attempts_state_hash")
        ),
    )
    op.create_index(
        "ix_youtube_oauth_attempts_expiry",
        "youtube_oauth_attempts", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_youtube_oauth_attempts_user_org",
        "youtube_oauth_attempts", ["user_id", "organization_id"], unique=False
    )

    op.create_table(
        "youtube_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("custom_url", sa.String(length=255), nullable=True),
        sa.Column("thumbnail_url", sa.String(length=2048), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("granted_scopes", sa.Text(), nullable=False),
        sa.Column("connected_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default="active", nullable=False
        ),
        sa.Column(
            "connected_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "length(trim(channel_id)) > 0",
            name=op.f("ck_youtube_channels_channel_id_not_empty"),
        ),
        sa.CheckConstraint(
            "length(trim(title)) > 0",
            name=op.f("ck_youtube_channels_title_not_empty"),
        ),
        sa.CheckConstraint(
            "key_version > 0",
            name=op.f("ck_youtube_channels_positive_key_version"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disconnected')",
            name=op.f("ck_youtube_channels_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_youtube_channels_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connected_by_user_id"], ["users.id"],
            name=op.f("fk_youtube_channels_connected_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_youtube_channels")),
        sa.UniqueConstraint(
            "organization_id", "channel_id",
            name="uq_youtube_channels_org_channel",
        ),
    )
    op.create_index(
        "ix_youtube_channels_organization_status",
        "youtube_channels", ["organization_id", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index(
        "ix_youtube_channels_organization_status", table_name="youtube_channels"
    )
    op.drop_table("youtube_channels")
    op.drop_index(
        "ix_youtube_oauth_attempts_user_org", table_name="youtube_oauth_attempts"
    )
    op.drop_index(
        "ix_youtube_oauth_attempts_expiry", table_name="youtube_oauth_attempts"
    )
    op.drop_table("youtube_oauth_attempts")

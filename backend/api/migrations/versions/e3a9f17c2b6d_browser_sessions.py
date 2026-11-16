"""Create server-managed browser sessions.

Revision ID: e3a9f17c2b6d
Revises: c9d3a71e4b20
"""

import sqlalchemy as sa
from alembic import op

revision = "e3a9f17c2b6d"
down_revision = "c9d3a71e4b20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(token_hash) = 64",
            name=op.f("ck_browser_sessions_token_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_browser_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_sessions")),
        sa.UniqueConstraint(
            "token_hash",
            name=op.f("uq_browser_sessions_token_hash"),
        ),
    )
    op.create_index(
        "ix_browser_sessions_user_revoked",
        "browser_sessions",
        ["user_id", "revoked_at"],
        unique=False,
    )
    op.create_index(
        "ix_browser_sessions_expiry",
        "browser_sessions",
        ["idle_expires_at", "absolute_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_browser_sessions_expiry", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_user_revoked", table_name="browser_sessions")
    op.drop_table("browser_sessions")

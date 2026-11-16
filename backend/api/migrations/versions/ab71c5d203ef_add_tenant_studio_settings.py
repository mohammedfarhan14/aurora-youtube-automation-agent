"""add tenant-scoped studio settings

Revision ID: ab71c5d203ef
Revises: 95cf17a2d4e8
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ab71c5d203ef"
down_revision: str | Sequence[str] | None = "95cf17a2d4e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "studio_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("studio_settings_updated_by_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "studio_settings_updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_organizations_studio_settings_updated_by_user",
        "organizations",
        "users",
        ["studio_settings_updated_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_organizations_studio_settings_updated_by_user",
        "organizations",
        type_="foreignkey",
    )
    op.drop_column("organizations", "studio_settings_updated_at")
    op.drop_column("organizations", "studio_settings_updated_by_user_id")
    op.drop_column("organizations", "studio_settings")

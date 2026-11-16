"""add production stage retry scheduling

Revision ID: 9f6a2d4c8b10
Revises: 4b8e2d91c6a3, 7d91c3a4e2f8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f6a2d4c8b10"
down_revision: str | Sequence[str] | None = ("4b8e2d91c6a3", "7d91c3a4e2f8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "production_stages",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_production_stages_queue_available",
        "production_stages",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_production_stages_queue_available",
        table_name="production_stages",
    )
    op.drop_column("production_stages", "next_attempt_at")

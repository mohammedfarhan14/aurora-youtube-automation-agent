"""persist production approval actor and timestamp

Revision ID: b7c4e1a92d60
Revises: 9f6a2d4c8b10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c4e1a92d60"
down_revision: str | Sequence[str] | None = "9f6a2d4c8b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "production_runs",
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "production_runs",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_production_runs_approved_by_user",
        "production_runs",
        "users",
        ["approved_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_production_runs_approval_actor_timestamp_pair",
        "production_runs",
        "(approved_by_user_id IS NULL AND approved_at IS NULL) OR "
        "(approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_production_runs_approval_actor_timestamp_pair",
        "production_runs",
        type_="check",
    )
    op.drop_constraint(
        "fk_production_runs_approved_by_user",
        "production_runs",
        type_="foreignkey",
    )
    op.drop_column("production_runs", "approved_at")
    op.drop_column("production_runs", "approved_by_user_id")

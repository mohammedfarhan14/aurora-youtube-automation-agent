"""record an explicit editorial rejection for rendered production runs

Revision ID: ce21f8b6d4a90
Revises: ab71c5d203ef
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ce21f8b6d4a90"
down_revision: str | Sequence[str] | None = "ab71c5d203ef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("production_runs", sa.Column("rejected_by_user_id", sa.Uuid(), nullable=True))
    op.add_column("production_runs", sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("production_runs", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_production_runs_rejected_by_user",
        "production_runs",
        "users",
        ["rejected_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(op.f("ck_production_runs_valid_status"), "production_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_production_runs_valid_status"),
        "production_runs",
        "status IN ('queued', 'planning', 'generating', 'verifying', "
        "'awaiting_review', 'approved', 'rejected', 'failed', 'cancelled')",
    )
    op.create_check_constraint(
        op.f("ck_production_runs_rejection_actor_timestamp_pair"),
        "production_runs",
        "(status = 'rejected' AND rejected_by_user_id IS NOT NULL AND rejected_at IS NOT NULL) OR "
        "(status <> 'rejected' AND rejected_by_user_id IS NULL AND rejected_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_production_runs_rejection_actor_timestamp_pair"), "production_runs", type_="check"
    )
    op.drop_constraint(op.f("ck_production_runs_valid_status"), "production_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_production_runs_valid_status"),
        "production_runs",
        "status IN ('queued', 'planning', 'generating', 'verifying', "
        "'awaiting_review', 'approved', 'failed', 'cancelled')",
    )
    op.drop_constraint(
        "fk_production_runs_rejected_by_user", "production_runs", type_="foreignkey"
    )
    op.drop_column("production_runs", "rejection_reason")
    op.drop_column("production_runs", "rejected_at")
    op.drop_column("production_runs", "rejected_by_user_id")

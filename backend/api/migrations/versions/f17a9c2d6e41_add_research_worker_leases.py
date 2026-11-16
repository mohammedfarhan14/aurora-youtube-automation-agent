"""add research worker leases and retry scheduling

Revision ID: f17a9c2d6e41
Revises: d53c6cdde7d7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f17a9c2d6e41"
down_revision: str | Sequence[str] | None = "d53c6cdde7d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_runs",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "research_runs",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "research_runs",
        sa.Column("lease_token", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "research_runs",
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_check_constraint(
        op.f("ck_research_runs_attempt_count_non_negative"),
        "research_runs",
        "attempt_count >= 0",
    )

    op.create_index(
        "ix_research_runs_queue_available",
        "research_runs",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_research_runs_lease_expiry",
        "research_runs",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_runs_lease_expiry",
        table_name="research_runs",
    )
    op.drop_index(
        "ix_research_runs_queue_available",
        table_name="research_runs",
    )
    op.drop_constraint(
        op.f("ck_research_runs_attempt_count_non_negative"),
        "research_runs",
        type_="check",
    )

    op.drop_column("research_runs", "lease_expires_at")
    op.drop_column("research_runs", "lease_token")
    op.drop_column("research_runs", "next_attempt_at")
    op.drop_column("research_runs", "attempt_count")

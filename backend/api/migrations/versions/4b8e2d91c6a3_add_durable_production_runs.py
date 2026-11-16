"""add durable production runs and stages

Revision ID: 4b8e2d91c6a3
Revises: f17a9c2d6e41
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4b8e2d91c6a3"
down_revision: str | Sequence[str] | None = "f17a9c2d6e41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_research_runs_id_organization",
        "research_runs",
        ["id", "organization_id"],
    )
    op.create_table(
        "production_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("research_run_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'planning', 'generating', 'verifying', 'awaiting_review', 'approved', 'failed', 'cancelled')",
            name=op.f("ck_production_runs_valid_status"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_production_runs_attempt_count_non_negative")),
        sa.CheckConstraint("length(trim(idempotency_key)) > 0", name=op.f("ck_production_runs_idempotency_key_not_empty")),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["research_run_id", "organization_id"],
            ["research_runs.id", "research_runs.organization_id"],
            name="fk_production_runs_research_run_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_production_runs")),
        sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_production_runs_organization_idempotency"),
    )
    op.create_index("ix_production_runs_org_created", "production_runs", ["organization_id", "created_at"])
    op.create_index("ix_production_runs_status_created", "production_runs", ["status", "created_at"])
    op.create_index("ix_production_runs_lease_expiry", "production_runs", ["status", "lease_expires_at"])

    op.create_table(
        "production_stages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("stage_key", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_manifest", sa.JSON(), nullable=False),
        sa.Column("output_manifest", sa.JSON(), nullable=False),
        sa.Column("provider_id", sa.String(length=100), nullable=True),
        sa.Column("model_id", sa.String(length=200), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=True),
        sa.Column("request_id", sa.String(length=200), nullable=True),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'cancelled')",
            name=op.f("ck_production_stages_valid_status"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_production_stages_attempt_count_non_negative")),
        sa.ForeignKeyConstraint(["production_run_id"], ["production_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_production_stages")),
        sa.UniqueConstraint("production_run_id", "stage_key", name="uq_production_stages_run_key"),
    )
    op.create_index("ix_production_stages_status_created", "production_stages", ["status", "created_at"])
    op.create_index("ix_production_stages_lease_expiry", "production_stages", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_production_stages_lease_expiry", table_name="production_stages")
    op.drop_index("ix_production_stages_status_created", table_name="production_stages")
    op.drop_table("production_stages")
    op.drop_index("ix_production_runs_lease_expiry", table_name="production_runs")
    op.drop_index("ix_production_runs_status_created", table_name="production_runs")
    op.drop_index("ix_production_runs_org_created", table_name="production_runs")
    op.drop_table("production_runs")
    op.drop_constraint(
        "uq_research_runs_id_organization",
        "research_runs",
        type_="unique",
    )

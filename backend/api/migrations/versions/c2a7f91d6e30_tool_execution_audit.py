"""persist permissioned tool execution audit events

Revision ID: c2a7f91d6e30
Revises: a6d9c3e1f742
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2a7f91d6e30"
down_revision: str | Sequence[str] | None = "a6d9c3e1f742"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_execution_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=200), nullable=False),
        sa.Column("skill_id", sa.String(length=120), nullable=False),
        sa.Column("skill_version", sa.String(length=32), nullable=False),
        sa.Column("tool_id", sa.String(length=120), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure')",
            name="ck_tool_execution_audit_valid_outcome",
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name="ck_tool_execution_audit_duration_non_negative",
        ),
        sa.CheckConstraint(
            "input_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_tool_execution_audit_input_digest_sha256",
        ),
        sa.CheckConstraint(
            "output_sha256 IS NULL OR output_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_tool_execution_audit_output_digest_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
            name="fk_tool_execution_audit_organization_id_organizations",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_tool_execution_audit_actor_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_execution_audit"),
    )
    op.create_index(
        "ix_tool_execution_audit_org_created",
        "tool_execution_audit",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_tool_execution_audit_request",
        "tool_execution_audit",
        ["organization_id", "request_id"],
    )
    op.create_index(
        "ix_tool_execution_audit_tool",
        "tool_execution_audit",
        ["organization_id", "tool_id", "created_at"],
    )
    # Audit event contents cannot be rewritten; tenant deletion and retention cleanup remain possible.
    op.execute(
        """
        CREATE FUNCTION prevent_tool_execution_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'tool execution audit records are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tool_execution_audit_immutable
        BEFORE UPDATE ON tool_execution_audit
        FOR EACH ROW EXECUTE FUNCTION prevent_tool_execution_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tool_execution_audit_immutable ON tool_execution_audit"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_tool_execution_audit_mutation()")
    op.drop_index("ix_tool_execution_audit_tool", table_name="tool_execution_audit")
    op.drop_index("ix_tool_execution_audit_request", table_name="tool_execution_audit")
    op.drop_index("ix_tool_execution_audit_org_created", table_name="tool_execution_audit")
    op.drop_table("tool_execution_audit")

"""add append-only versioned story artifacts

Revision ID: 7d91c3a4e2f8
Revises: 4b8e2d91c6a3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d91c3a4e2f8"
down_revision: str | Sequence[str] | None = "4b8e2d91c6a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "story_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("production_stage_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_story_artifacts_version_positive")),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_story_artifacts_content_hash_sha256"),
        ),
        sa.CheckConstraint(
            "artifact_type IN ('story_draft', 'story_verified')",
            name=op.f("ck_story_artifacts_valid_artifact_type"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"], ["production_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["production_stage_id"], ["production_stages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_artifacts")),
        sa.UniqueConstraint(
            "production_run_id",
            "artifact_type",
            "version",
            name="uq_story_artifacts_run_type_version",
        ),
    )
    op.create_index(
        "ix_story_artifacts_org_run_created",
        "story_artifacts",
        ["organization_id", "production_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_story_artifacts_org_run_created", table_name="story_artifacts")
    op.drop_table("story_artifacts")

"""add tenant-bound media artifact metadata registry

Revision ID: d4e8b2a6f190
Revises: c2a7f91d6e30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e8b2a6f190"
down_revision: str | Sequence[str] | None = "c2a7f91d6e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Composite unique keys let the artifact table enforce tenant/run/stage
    # relationships in PostgreSQL rather than relying only on application checks.
    op.create_unique_constraint(
        "uq_production_runs_id_org",
        "production_runs",
        ["id", "organization_id"],
    )
    op.create_unique_constraint(
        "uq_production_stages_id_run",
        "production_stages",
        ["id", "production_run_id"],
    )
    op.create_table(
        "media_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("production_stage_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_kind", sa.String(length=30), nullable=False),
        sa.Column("storage_backend", sa.String(length=20), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "artifact_kind IN ('audio', 'image', 'video', 'captions', 'thumbnail')",
            name="ck_media_artifacts_valid_artifact_kind",
        ),
        sa.CheckConstraint(
            "storage_backend IN ('local', 's3')",
            name="ck_media_artifacts_valid_storage_backend",
        ),
        sa.CheckConstraint(
            "length(trim(storage_key)) > 0",
            name="ck_media_artifacts_storage_key_not_empty",
        ),
        sa.CheckConstraint(
            "length(trim(mime_type)) > 0",
            name="ck_media_artifacts_mime_type_not_empty",
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name="ck_media_artifacts_byte_size_positive",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_media_artifacts_valid_sha256",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_media_artifacts_duration_non_negative",
        ),
        sa.CheckConstraint(
            "(width IS NULL AND height IS NULL) OR "
            "(width IS NOT NULL AND height IS NOT NULL AND width > 0 AND height > 0)",
            name="ck_media_artifacts_dimensions_pair_positive",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id", "organization_id"],
            ["production_runs.id", "production_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_media_artifacts_run_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["production_stage_id", "production_run_id"],
            ["production_stages.id", "production_stages.production_run_id"],
            ondelete="CASCADE",
            name="fk_media_artifacts_stage_run",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_media_artifacts"),
        sa.UniqueConstraint(
            "organization_id",
            "storage_backend",
            "storage_key",
            name="uq_media_artifacts_org_storage_key",
        ),
    )
    op.create_index(
        "ix_media_artifacts_run_created",
        "media_artifacts",
        ["organization_id", "production_run_id", "created_at"],
    )
    op.create_index(
        "ix_media_artifacts_stage",
        "media_artifacts",
        ["production_stage_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_media_artifacts_stage", table_name="media_artifacts")
    op.drop_index("ix_media_artifacts_run_created", table_name="media_artifacts")
    op.drop_table("media_artifacts")
    op.drop_constraint(
        "uq_production_stages_id_run",
        "production_stages",
        type_="unique",
    )
    op.drop_constraint(
        "uq_production_runs_id_org",
        "production_runs",
        type_="unique",
    )

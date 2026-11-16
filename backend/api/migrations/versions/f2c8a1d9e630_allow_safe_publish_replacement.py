"""Allow a safely cancelled publishing intent to be replaced after human review.

Revision ID: f2c8a1d9e630
Revises: c2a7e91f4b30
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f2c8a1d9e630"
down_revision: str | Sequence[str] | None = "c2a7e91f4b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_youtube_publish_jobs_production_run",
        "youtube_publish_jobs",
        type_="unique",
    )
    op.create_index(
        "ix_youtube_publish_jobs_production_run",
        "youtube_publish_jobs",
        ["production_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT production_run_id
                FROM youtube_publish_jobs
                GROUP BY production_run_id
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot restore one-job-per-run constraint while replacement jobs exist.';
            END IF;
        END $$;
        """
    )
    op.drop_index(
        "ix_youtube_publish_jobs_production_run",
        table_name="youtube_publish_jobs",
    )
    op.create_unique_constraint(
        "uq_youtube_publish_jobs_production_run",
        "youtube_publish_jobs",
        ["production_run_id"],
    )

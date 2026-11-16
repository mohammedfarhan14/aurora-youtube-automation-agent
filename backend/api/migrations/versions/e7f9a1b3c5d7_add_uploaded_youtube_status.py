"""add uploaded state for private YouTube videos

Revision ID: e7f9a1b3c5d7
Revises: d6e8f0a2b4c6
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e7f9a1b3c5d7"
down_revision: str | Sequence[str] | None = "d6e8f0a2b4c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "valid_status",
        "youtube_publish_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "valid_status",
        "youtube_publish_jobs",
        "status IN ('queued', 'initiating', 'uploading', 'processing', "
        "'uploaded', 'published', 'failed', 'cancelled', 'reconciliation_required')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "valid_status",
        "youtube_publish_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "valid_status",
        "youtube_publish_jobs",
        "status IN ('queued', 'initiating', 'uploading', 'processing', "
        "'published', 'failed', 'cancelled', 'reconciliation_required')",
    )

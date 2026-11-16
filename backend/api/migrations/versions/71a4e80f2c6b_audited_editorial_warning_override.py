"""record explicit owner approval when uploading through editorial warnings

Revision ID: 71a4e80f2c6b
Revises: ce21f8b6d4a90
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "71a4e80f2c6b"
down_revision: str | Sequence[str] | None = "ce21f8b6d4a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "production_runs",
        sa.Column("approval_warning_override", sa.JSON(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("production_runs", "approval_warning_override")

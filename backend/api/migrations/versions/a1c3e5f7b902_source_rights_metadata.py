"""persist source-level rights metadata

Revision ID: a1c3e5f7b902
Revises: f8b2c4d6e810
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c3e5f7b902"
down_revision: str | Sequence[str] | None = "f8b2c4d6e810"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_sources",
        sa.Column(
            "rights_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("research_sources", "rights_metadata")

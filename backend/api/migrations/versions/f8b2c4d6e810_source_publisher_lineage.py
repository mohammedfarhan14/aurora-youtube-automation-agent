"""persist source publisher lineage and independence groups

Revision ID: f8b2c4d6e810
Revises: d4e8b2a6f190
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8b2c4d6e810"
down_revision: str | Sequence[str] | None = "d4e8b2a6f190"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_sources",
        sa.Column("publisher_domain", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "research_sources",
        sa.Column(
            "source_class",
            sa.String(length=40),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "research_sources",
        sa.Column("independence_group_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "research_sources",
        sa.Column(
            "independence_basis",
            sa.String(length=40),
            server_default="unassessed",
            nullable=False,
        ),
    )
    op.add_column(
        "research_sources",
        sa.Column(
            "classification_version",
            sa.String(length=20),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_research_sources_valid_source_class",
        "research_sources",
        "source_class IN ('scholarly', 'journalism', 'video', 'reference', "
        "'community', 'official_domain', 'unknown')",
    )
    op.create_check_constraint(
        "ck_research_sources_valid_independence_basis",
        "research_sources",
        "independence_basis IN ('unassessed', 'unique_source', 'publisher_domain', "
        "'exact_content', 'near_duplicate_content')",
    )
    op.create_index(
        "ix_research_sources_run_independence",
        "research_sources",
        ["research_run_id", "independence_group_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_sources_run_independence",
        table_name="research_sources",
    )
    op.drop_constraint(
        "ck_research_sources_valid_independence_basis",
        "research_sources",
        type_="check",
    )
    op.drop_constraint(
        "ck_research_sources_valid_source_class",
        "research_sources",
        type_="check",
    )
    op.drop_column("research_sources", "classification_version")
    op.drop_column("research_sources", "independence_basis")
    op.drop_column("research_sources", "independence_group_key")
    op.drop_column("research_sources", "source_class")
    op.drop_column("research_sources", "publisher_domain")

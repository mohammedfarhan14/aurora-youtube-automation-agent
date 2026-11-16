"""add tenant source rights profiles and audited reviews

Revision ID: c4d6e8f0a2b4
Revises: a1c3e5f7b902
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4d6e8f0a2b4"
down_revision: str | Sequence[str] | None = "a1c3e5f7b902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_source_rights_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_url_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "rights_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "canonical_url_hash ~ '^[a-f0-9]{64}$'",
            name="canonical_url_hash_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_org_source_rights_profiles_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "canonical_url_hash",
            name="uq_org_source_rights_profile_org_url_hash",
        ),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_org_source_rights_profiles_id_org",
        ),
    )
    op.create_index(
        "ix_org_source_rights_profiles_org_updated",
        "organization_source_rights_profiles",
        ["organization_id", "updated_at"],
    )

    op.create_table(
        "research_source_rights_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("rights_profile_id", sa.Uuid(), nullable=False),
        sa.Column("research_run_id", sa.Uuid(), nullable=False),
        sa.Column("research_source_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("review_notes", sa.Text(), nullable=False),
        sa.Column(
            "evidence_urls",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "previous_rights_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "resulting_rights_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('cleared', 'restricted')",
            name="valid_decision",
        ),
        sa.CheckConstraint(
            "length(trim(review_notes)) > 0",
            name="review_notes_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["rights_profile_id", "organization_id"],
            [
                "organization_source_rights_profiles.id",
                "organization_source_rights_profiles.organization_id",
            ],
            name="fk_source_rights_reviews_profile_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name="fk_source_rights_reviews_reviewer",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_rights_reviews_source_created",
        "research_source_rights_reviews",
        ["research_source_id", "created_at"],
    )
    op.create_index(
        "ix_source_rights_reviews_profile_created",
        "research_source_rights_reviews",
        ["rights_profile_id", "created_at"],
    )
    op.create_index(
        "ix_source_rights_reviews_org_created",
        "research_source_rights_reviews",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_rights_reviews_org_created",
        table_name="research_source_rights_reviews",
    )
    op.drop_index(
        "ix_source_rights_reviews_source_created",
        table_name="research_source_rights_reviews",
    )
    op.drop_index(
        "ix_source_rights_reviews_profile_created",
        table_name="research_source_rights_reviews",
    )
    op.drop_table("research_source_rights_reviews")
    op.drop_index(
        "ix_org_source_rights_profiles_org_updated",
        table_name="organization_source_rights_profiles",
    )
    op.drop_table("organization_source_rights_profiles")

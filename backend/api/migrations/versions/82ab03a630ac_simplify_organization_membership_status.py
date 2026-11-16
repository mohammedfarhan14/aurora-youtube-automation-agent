"""simplify organization membership status

Revision ID: 82ab03a630ac
Revises: bb82afdb1c63
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "82ab03a630ac"
down_revision: str | Sequence[str] | None = "bb82afdb1c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""

    op.drop_constraint(
        op.f("ck_organization_members_valid_status"),
        "organization_members",
        type_="check",
    )

    op.create_check_constraint(
        op.f("ck_organization_members_valid_status"),
        "organization_members",
        "status IN ('active', 'suspended')",
    )

    op.alter_column(
        "organization_members",
        "status",
        server_default="active",
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_constraint(
        op.f("ck_organization_members_valid_status"),
        "organization_members",
        type_="check",
    )

    op.create_check_constraint(
        op.f("ck_organization_members_valid_status"),
        "organization_members",
        "status IN ('active', 'invited', 'suspended')",
    )

    op.alter_column(
        "organization_members",
        "status",
        server_default=None,
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )

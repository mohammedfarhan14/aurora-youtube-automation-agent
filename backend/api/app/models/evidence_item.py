from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        CheckConstraint(
            "length(trim(excerpt)) > 0 AND length(excerpt) <= 10000",
            name="valid_excerpt",
        ),
        UniqueConstraint(
            "research_source_id",
            "content_hash",
            name="uq_evidence_items_source_content_hash",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    research_source_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_sources.id", ondelete="CASCADE"),
        nullable=False,
    )

    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Optional locator within the source: section heading, page, timestamp, etc.
    locator: Mapped[str | None] = mapped_column(String(500), nullable=True)

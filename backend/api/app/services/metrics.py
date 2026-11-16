"""Low-cardinality Prometheus metrics backed by durable workflow state."""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.engine import get_session_factory
from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage
from app.models.research_run import ResearchRun
from app.models.youtube_publishing import YouTubePublishJob


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


async def render_prometheus_metrics(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> str:
    """Render aggregate workflow health without exposing tenant or user identifiers."""
    session_factory = session_factory or get_session_factory()
    lines = [
        "# HELP aurora_research_runs_recent Research runs created in the last 30 days.",
        "# TYPE aurora_research_runs_recent gauge",
        "# HELP aurora_production_runs_recent Production runs created in the last 30 days.",
        "# TYPE aurora_production_runs_recent gauge",
        "# HELP aurora_production_stages_recent Production stages created in the last 30 days.",
        "# TYPE aurora_production_stages_recent gauge",
        (
            "# HELP aurora_youtube_publish_jobs_recent "
            "YouTube publishing jobs created in the last 30 days."
        ),
        "# TYPE aurora_youtube_publish_jobs_recent gauge",
        (
            "# HELP aurora_worker_failures_recent "
            "Terminal worker failures in the last 30 days by queue."
        ),
        "# TYPE aurora_worker_failures_recent gauge",
        "# HELP aurora_worker_expired_leases Current expired worker leases by queue.",
        "# TYPE aurora_worker_expired_leases gauge",
        (
            "# HELP aurora_production_stage_latency_seconds_avg "
            "Average completed stage latency over seven days."
        ),
        "# TYPE aurora_production_stage_latency_seconds_avg gauge",
        (
            "# HELP aurora_youtube_publish_failures_recent "
            "Failed or reconciliation-required publishing jobs created in the last 30 days."
        ),
        "# TYPE aurora_youtube_publish_failures_recent gauge",
        (
            "# HELP aurora_youtube_upload_failures_recent "
            "Failed or reconciliation-required uploads in the last 30 days."
        ),
        "# TYPE aurora_youtube_upload_failures_recent gauge",
    ]

    async with session_factory() as session:
        cutoff_30d = func.clock_timestamp() - timedelta(days=30)
        cutoff_7d = func.clock_timestamp() - timedelta(days=7)

        research = (
            await session.execute(
                select(ResearchRun.status, func.count())
                .where(ResearchRun.created_at >= cutoff_30d)
                .group_by(ResearchRun.status)
            )
        ).all()
        production = (
            await session.execute(
                select(ProductionRun.status, func.count())
                .where(ProductionRun.created_at >= cutoff_30d)
                .group_by(ProductionRun.status)
            )
        ).all()
        stages = (
            await session.execute(
                select(ProductionStage.stage_key, ProductionStage.status, func.count())
                .where(ProductionStage.created_at >= cutoff_30d)
                .group_by(ProductionStage.stage_key, ProductionStage.status)
            )
        ).all()
        publish_jobs = (
            await session.execute(
                select(YouTubePublishJob.status, func.count())
                .where(YouTubePublishJob.created_at >= cutoff_30d)
                .group_by(YouTubePublishJob.status)
            )
        ).all()

        for status, count in research:
            lines.append(
                f'aurora_research_runs_recent{{status="{_label(status)}"}} {count}'
            )
        for status, count in production:
            lines.append(
                f'aurora_production_runs_recent{{status="{_label(status)}"}} {count}'
            )
        for stage_key, status, count in stages:
            lines.append(
                "aurora_production_stages_recent"
                f'{{stage_key="{_label(stage_key)}",status="{_label(status)}"}} {count}'
            )
        for status, count in publish_jobs:
            lines.append(
                f'aurora_youtube_publish_jobs_recent{{status="{_label(status)}"}} {count}'
            )

        failed_research = sum(count for status, count in research if status == "failed")
        failed_production = sum(count for status, count in production if status == "failed")
        failed_stages = sum(
            count for _, status, count in stages if status == "failed"
        )
        failed_publish_jobs = sum(
            count
            for status, count in publish_jobs
            if status in {"failed", "reconciliation_required"}
        )
        lines.extend(
            [
                f'aurora_worker_failures_recent{{queue="research"}} {failed_research}',
                f'aurora_worker_failures_recent{{queue="production"}} {failed_production}',
                f'aurora_worker_failures_recent{{queue="production_stage"}} {failed_stages}',
                f'aurora_worker_failures_recent{{queue="youtube_publish"}} {failed_publish_jobs}',
                f"aurora_youtube_upload_failures_recent {failed_publish_jobs}",
            ]
        )

        expired_research = await session.scalar(
            select(func.count())
            .select_from(ResearchRun)
            .where(
                ResearchRun.status == "researching",
                ResearchRun.lease_expires_at <= func.clock_timestamp(),
            )
        )
        expired_stages = await session.scalar(
            select(func.count())
            .select_from(ProductionStage)
            .where(
                ProductionStage.status == "running",
                ProductionStage.lease_expires_at <= func.clock_timestamp(),
            )
        )
        expired_publish = await session.scalar(
            select(func.count())
            .select_from(YouTubePublishJob)
            .where(
                YouTubePublishJob.status.in_(
                    {"initiating", "uploading", "processing"}
                ),
                YouTubePublishJob.lease_expires_at <= func.clock_timestamp(),
            )
        )
        lines.extend(
            [
                f'aurora_worker_expired_leases{{queue="research"}} {expired_research or 0}',
                f'aurora_worker_expired_leases{{queue="production"}} {expired_stages or 0}',
                f'aurora_worker_expired_leases{{queue="youtube_publish"}} {expired_publish or 0}',
            ]
        )

        latency = (
            await session.execute(
                select(
                    ProductionStage.stage_key,
                    func.avg(
                        func.extract(
                            "epoch",
                            ProductionStage.completed_at - ProductionStage.started_at,
                        )
                    ),
                )
                .where(
                    ProductionStage.status == "completed",
                    ProductionStage.started_at.is_not(None),
                    ProductionStage.completed_at.is_not(None),
                    ProductionStage.completed_at >= cutoff_7d,
                )
                .group_by(ProductionStage.stage_key)
            )
        ).all()
        for stage_key, seconds in latency:
            lines.append(
                "aurora_production_stage_latency_seconds_avg"
                f'{{stage_key="{_label(stage_key)}"}} {float(seconds or 0):.3f}'
            )

        failed_publish_jobs_total = await session.scalar(
            select(func.count())
            .select_from(YouTubePublishJob)
            .where(
                YouTubePublishJob.created_at >= cutoff_30d,
                YouTubePublishJob.status.in_({"failed", "reconciliation_required"}),
            )
        )
        lines.append(
            f"aurora_youtube_publish_failures_recent {failed_publish_jobs_total or 0}"
        )

    return "\n".join(lines) + "\n"

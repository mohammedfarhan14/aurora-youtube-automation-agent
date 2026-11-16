from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from app.services.youtube_channel_settings import next_schedule_occurrence


def test_next_schedule_occurrence_uses_configured_local_timezone():
    now = datetime(2026, 11, 12, 2, 0, tzinfo=UTC)

    occurrence = next_schedule_occurrence(
        now=now,
        timezone_name="Asia/Kolkata",
        posting_time=time(9, 0),
    )

    assert occurrence == datetime(2026, 11, 12, 3, 30, tzinfo=UTC)


def test_next_schedule_occurrence_moves_to_next_local_day_when_time_passed():
    now = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)

    occurrence = next_schedule_occurrence(
        now=now,
        timezone_name="Asia/Kolkata",
        posting_time=time(9, 0),
    )

    assert occurrence == datetime(2026, 11, 13, 3, 30, tzinfo=UTC)


def test_next_schedule_occurrence_handles_ambiguous_daylight_saving_fold():
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=1)

    occurrence = next_schedule_occurrence(
        now=now,
        timezone_name="America/New_York",
        posting_time=time(1, 30),
    )

    assert occurrence == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_next_schedule_occurrence_normalizes_nonexistent_daylight_saving_time():
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 3, 8, 0, 0, tzinfo=zone)

    occurrence = next_schedule_occurrence(
        now=now,
        timezone_name="America/New_York",
        posting_time=time(2, 30),
    )

    assert occurrence == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)

from datetime import datetime, timedelta, timezone

from Forge.core.oracle_research_data import OracleResearchDataProvider


def test_completed_weekly_bar_is_fresh_early_in_the_new_week():
    now = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
    last_week_open = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)

    assert last_week_open < now - timedelta(days=7)
    assert not OracleResearchDataProvider._bar_is_stale(
        last_week_open, 7 * 24 * 60 * 60 * 1000, now
    )


def test_weekly_history_older_than_freshness_limit_is_stale():
    now = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
    stale_week_open = datetime(2026, 10, 12, 0, tzinfo=timezone.utc)

    assert OracleResearchDataProvider._bar_is_stale(
        stale_week_open, 7 * 24 * 60 * 60 * 1000, now
    )

from datetime import UTC, datetime, timedelta

from client.window import parse_since, resolve_window, split_date_windows


def test_parse_since_naive_iso_is_normalized_to_utc():
    # Offset-less ISO input must come back tz-aware UTC, else it can't be compared with the
    # tz-aware run-start and split_date_windows raises TypeError.
    result = parse_since("2026-01-01T00:00:00")
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)


def test_parse_since_keeps_explicit_offset():
    assert parse_since("2026-01-01T00:00:00+00:00").utcoffset() == timedelta(0)


def test_parse_since_converts_nonutc_offset_to_utc():
    # A non-UTC offset must be converted to UTC (same instant), not passed through — so request
    # params use one canonical representation.
    result = parse_since("2026-01-01T00:00:00+02:00")
    assert result.utcoffset() == timedelta(0)
    assert result == datetime(2025, 12, 31, 22, 0, tzinfo=UTC)


def test_split_under_max_returns_single_window():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    assert split_date_windows(start, end, max_days=365) == [(start, end)]


def test_split_over_max_chunks_by_max_days():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)  # ~731 days
    windows = split_date_windows(start, end, max_days=365)
    assert len(windows) == 3
    assert windows[0][0] == start
    assert windows[-1][1] == end
    # Calendar-day windows: WFM endDate is inclusive, so each non-final window ends the day BEFORE
    # the next begins (no shared boundary day => no duplicate rows), and they leave no calendar gap.
    for prev, nxt in zip(windows, windows[1:], strict=False):
        assert prev[1] < nxt[0]  # not contiguous in datetime...
        assert nxt[0].date() - prev[1].date() == timedelta(days=1)  # ...but adjacent by one calendar day


def test_day_windows_do_not_share_a_boundary_day():
    # Regression: with an inclusive endDate, contiguous windows would fetch the boundary day twice
    # (duplicate rows for keyless resources). Adjacent windows must not share a calendar date.
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 4, 1, tzinfo=UTC)  # 90 days
    windows = split_date_windows(start, end, max_days=30)
    assert len(windows) == 3
    end_dates = {w[1].date() for w in windows}
    start_dates = {w[0].date() for w in windows}
    assert end_dates.isdisjoint(start_dates)  # no day is both an end and a start
    assert windows[0][0] == start
    assert windows[-1][1] == end


def test_minute_windows_stay_contiguous():
    # Punch minute-cap windows keep the original contiguous half-open datetime behaviour.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)  # 150 minutes
    windows = split_date_windows(start, end, max_minutes=60)
    assert len(windows) == 3
    for prev, nxt in zip(windows, windows[1:], strict=False):
        assert prev[1] == nxt[0]  # contiguous
    assert windows[-1][1] == end


def test_resolve_window_uses_since_and_until_verbatim():
    since_iso, until_iso = resolve_window("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00")
    assert since_iso == "2026-01-01T00:00:00+00:00"
    assert until_iso == "2026-02-01T00:00:00+00:00"


def test_resolve_window_no_until_defaults_upper_to_now():
    before = datetime.now(UTC)
    since_iso, until_iso = resolve_window("2026-01-01T00:00:00+00:00", None)
    assert since_iso == "2026-01-01T00:00:00+00:00"
    assert datetime.fromisoformat(until_iso) >= before  # upper bound is the run start (now)


def test_resolve_window_no_since_is_unbounded_lower():
    since_iso, until_iso = resolve_window(None, "2026-02-01T00:00:00+00:00")
    assert since_iso is None
    assert until_iso == "2026-02-01T00:00:00+00:00"


def test_resolve_window_is_stateless_end_date_does_not_freeze():
    # Regression for the watermark-freeze bug: with an absolute End Date the window is identical on
    # every run — there is no persisted watermark to collapse it to an empty [End, End] window.
    first = resolve_window("2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00")
    second = resolve_window("2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00")
    assert first == second
    assert first == ("2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00")

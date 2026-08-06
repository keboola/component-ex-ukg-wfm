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
    # contiguous, non-overlapping
    for prev, nxt in zip(windows, windows[1:], strict=False):
        assert prev[1] == nxt[0]


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

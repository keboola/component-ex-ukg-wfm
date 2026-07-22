from datetime import UTC, datetime, timedelta

from client.window import compute_window, parse_since, split_date_windows


def test_parse_since_naive_iso_is_normalized_to_utc():
    # Offset-less ISO input must come back tz-aware UTC, else it can't be compared with the
    # tz-aware run-start and split_date_windows raises TypeError.
    result = parse_since("2026-01-01T00:00:00")
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)


def test_parse_since_keeps_explicit_offset():
    assert parse_since("2026-01-01T00:00:00+00:00").utcoffset() == timedelta(0)


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


def test_compute_window_first_run_uses_since():
    params, run_started = compute_window({}, "start", "2026-01-01T00:00:00+00:00")
    assert params["start_since"] == "2026-01-01T00:00:00+00:00"
    assert "start_until" in params
    assert run_started


def test_compute_window_end_date_bounds_upper_and_watermark():
    # An explicit End Date (`until`) sets the upper bound AND becomes the persisted watermark,
    # so the next run continues from there rather than from "now".
    params, watermark = compute_window({}, "start", "2026-01-01T00:00:00+00:00", until="2026-02-01T00:00:00+00:00")
    assert params["start_since"] == "2026-01-01T00:00:00+00:00"
    assert params["start_until"] == "2026-02-01T00:00:00+00:00"
    assert watermark == "2026-02-01T00:00:00+00:00"


def test_compute_window_no_end_date_defaults_upper_to_now():
    before = datetime.now(UTC)
    params, watermark = compute_window({}, "start", None)
    upper = datetime.fromisoformat(params["start_until"])
    assert upper >= before  # upper bound is the run start (now)
    assert watermark == params["start_until"]

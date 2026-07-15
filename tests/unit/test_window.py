from datetime import UTC, datetime

from client.window import STATE_LAST_RUN, compute_window, split_date_windows


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
    params, run_started = compute_window({}, "start", "2026-01-01T00:00:00+00:00", 0)
    assert params["start_since"] == "2026-01-01T00:00:00+00:00"
    assert "start_until" in params
    assert run_started


def test_compute_window_applies_overlap_to_watermark():
    state = {STATE_LAST_RUN: "2026-06-01T00:00:00+00:00"}
    params, _ = compute_window(state, "start", None, 3600)
    assert params["start_since"] == "2026-05-31T23:00:00+00:00"

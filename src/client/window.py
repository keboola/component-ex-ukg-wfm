from datetime import UTC, datetime, timedelta
from typing import Any

import dateparser
from keboola.component.exceptions import UserException

STATE_LAST_RUN = "last_run"

_DATEPARSER_SETTINGS = {
    "TIMEZONE": "UTC",
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DATES_FROM": "past",
}


def parse_since(value: str) -> datetime:
    """Parse an ISO 8601 or relative-phrase datetime string into a timezone-aware UTC datetime.

    Fast-path: datetime.fromisoformat for ISO strings.
    Fallback: dateparser for natural-language phrases (e.g. 'yesterday', '3 days ago').
    Raises UserException if neither parser yields a result.
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass

    parsed = dateparser.parse(value, settings=_DATEPARSER_SETTINGS)
    if parsed is None:
        raise UserException(
            f"Cannot parse '{value}' as a date — use ISO 8601 (e.g. '2026-01-01T00:00:00+00:00') "
            "or a relative phrase (e.g. 'yesterday', '3 days ago', 'now')."
        )
    return parsed


def compute_window(
    state: dict[str, Any], date_field: str, since: str | None, overlap_seconds: int
) -> tuple[dict[str, Any], str]:
    """Return (query-params, run_started_iso). Capture run-start BEFORE fetch; persist AFTER write."""
    run_started = datetime.now(UTC)
    params: dict = {f"{date_field}_until": run_started.isoformat()}

    watermark = state.get(STATE_LAST_RUN)
    lower_str = watermark or since
    if lower_str:
        if watermark:
            # State watermark is always written as exact ISO — use fromisoformat directly.
            try:
                lower = datetime.fromisoformat(watermark)
            except ValueError as e:
                raise UserException(
                    f"Invalid ISO 8601 datetime value '{watermark}': {e}"
                ) from e
        else:
            # User-supplied since: supports both ISO and relative phrases.
            lower = parse_since(since)  # type: ignore[arg-type]
        if watermark and overlap_seconds:
            lower = lower - timedelta(seconds=overlap_seconds)
        params[f"{date_field}_since"] = lower.isoformat()
    return params, run_started.isoformat()


def resolve_window(
    state: dict[str, Any],
    date_field: str,
    since: str | None,
    overlap_seconds: int,
    is_effective_incremental: bool,
) -> tuple[str | None, str | None, str]:
    """Return (since_iso, until_iso, run_started_iso) for a resource with a date field.

    When the run is *effectively* incremental (PK present + incremental_load) the state
    watermark is read and used as the lower bound (minus overlap) → [last_run - overlap, now].

    Otherwise (no PK, or full_load) the state watermark is ignored entirely and the configured
    `since` is applied as the lower bound on EVERY run → [since, now] (or unbounded if no `since`).
    This is a full refresh each run: no window shrink, no data loss.
    """
    window_state = state if is_effective_incremental else {}
    params, run_started = compute_window(window_state, date_field, since, overlap_seconds)
    return params.get(f"{date_field}_since"), params.get(f"{date_field}_until"), run_started


def split_date_windows(
    start: datetime, end: datetime, max_days: int = 365
) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into contiguous sub-windows each spanning at most max_days."""
    if end <= start:
        return []
    span = timedelta(days=max_days)
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + span, end)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows

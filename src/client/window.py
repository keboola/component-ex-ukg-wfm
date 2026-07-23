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
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is not None:
        # Normalize to UTC to honor the return contract. fromisoformat returns a naive datetime for
        # offset-less input (e.g. "2026-01-01T00:00:00") — assume UTC; an explicit non-UTC offset
        # (e.g. "...+02:00") is converted to UTC — the same instant, but one canonical representation
        # in request params and persisted watermarks, and comparable with the timezone-aware
        # run-start (a naive/aware comparison would raise TypeError downstream in split_date_windows).
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    parsed = dateparser.parse(value, settings=_DATEPARSER_SETTINGS)
    if parsed is None:
        raise UserException(
            f"Cannot parse '{value}' as a date — use ISO 8601 (e.g. '2026-01-01T00:00:00+00:00') "
            "or a relative phrase (e.g. 'yesterday', '3 days ago', 'now')."
        )
    # dateparser already returns UTC-aware (TIMEZONE=UTC), but normalize for a single guaranteed contract.
    return parsed.astimezone(UTC)


def compute_window(
    state: dict[str, Any], date_field: str, since: str | None, until: str | None = None
) -> tuple[dict[str, Any], str]:
    """Return (query-params, watermark_iso). Capture run-start BEFORE fetch; persist AFTER write.

    The upper bound is the configured `until` (End Date, ISO or relative phrase) when set, else the
    run start (now). The returned watermark is that same upper bound, so the next run continues from
    where this one stopped.
    """
    run_started = datetime.now(UTC)
    upper = parse_since(until) if until else run_started
    params: dict = {f"{date_field}_until": upper.isoformat()}

    watermark = state.get(STATE_LAST_RUN)
    lower_str = watermark or since
    if lower_str:
        if watermark:
            # State watermark is always written as exact ISO — use fromisoformat directly.
            try:
                lower = datetime.fromisoformat(watermark)
            except ValueError as e:
                raise UserException(f"Invalid ISO 8601 datetime value '{watermark}': {e}") from e
        else:
            # User-supplied since: supports both ISO and relative phrases.
            # In this branch watermark is falsy, so lower_str == since and is a non-empty str.
            lower = parse_since(lower_str)
        params[f"{date_field}_since"] = lower.isoformat()
    return params, upper.isoformat()


def resolve_window(
    state: dict[str, Any],
    date_field: str,
    since: str | None,
    is_effective_incremental: bool,
    until: str | None = None,
) -> tuple[str | None, str | None, str]:
    """Return (since_iso, until_iso, watermark_iso) for a resource with a date field.

    The third element is the window's upper bound (the configured `until` when set, else the run
    start) — i.e. the value to persist as the next run's watermark, NOT the run-start timestamp.

    When the run is *effectively* incremental (PK present + incremental_load) the state
    watermark is read and used as the lower bound → [last_run, now].

    Otherwise (no PK, or full_load) the state watermark is ignored entirely and the configured
    `since` is applied as the lower bound on EVERY run → [since, now] (or unbounded if no `since`).
    This is a full refresh each run: no window shrink, no data loss.
    """
    window_state = state if is_effective_incremental else {}
    params, watermark_iso = compute_window(window_state, date_field, since, until)
    return params.get(f"{date_field}_since"), params.get(f"{date_field}_until"), watermark_iso


def split_date_windows(
    start: datetime, end: datetime, max_days: int = 365, max_minutes: int = 0
) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into contiguous sub-windows each spanning at most the given bound.

    max_minutes > 0 takes precedence (minute-granular windows, e.g. the <= 60-minute punches cap);
    otherwise the window is split by max_days.
    """
    if end <= start:
        return []
    span = timedelta(minutes=max_minutes) if max_minutes > 0 else timedelta(days=max_days)
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + span, end)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows

from datetime import UTC, datetime, timedelta

import dateparser
from keboola.component.exceptions import UserException

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
        # in request params, and comparable with the timezone-aware run-start (a naive/aware
        # comparison would raise TypeError downstream in split_date_windows).
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    parsed = dateparser.parse(value, settings=_DATEPARSER_SETTINGS)
    if parsed is None:
        raise UserException(
            f"Cannot parse '{value}' as a date — use ISO 8601 (e.g. '2026-01-01T00:00:00+00:00') "
            "or a relative phrase (e.g. 'yesterday', '3 days ago', 'now')."
        )
    # dateparser already returns UTC-aware (TIMEZONE=UTC), but normalize for a single guaranteed contract.
    return parsed.astimezone(UTC)


def resolve_window(since: str | None, until: str | None = None) -> tuple[str | None, str]:
    """Return (since_iso, until_iso) for a date-windowed resource, driven purely by config.

    Lower bound = the configured Start Date (`since`) when set, else unbounded (None).
    Upper bound = the configured End Date (`until`) when set, else the run start (now).

    The window is recomputed from config on EVERY run — there is no persisted watermark, so Load
    Type (full vs incremental) never changes what is fetched, only how Storage writes it. Both
    bounds are normalised to canonical UTC ISO (see parse_since) so they stay comparable.
    """
    upper = parse_since(until) if until else datetime.now(UTC)
    lower = parse_since(since) if since else None
    return (lower.isoformat() if lower is not None else None), upper.isoformat()


def split_date_windows(
    start: datetime, end: datetime, max_days: int = 365, max_minutes: int = 0
) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into sub-windows each spanning at most the given bound.

    max_minutes > 0 takes precedence (minute-granular windows, e.g. the <= 60-minute punches cap):
    these stay CONTIGUOUS half-open datetime windows.

    Otherwise the window is split by max_days into CALENDAR-DAY sub-windows. WFM's calendar
    `dateRange.endDate` is INCLUSIVE (VERIFIED live: a read with endDate=D returns rows dated D; with
    endDate=D-1 they disappear), so contiguous windows sharing a boundary day would fetch that day
    twice — duplicating rows for keyless resources. Each non-final day-window therefore ends one day
    before the next begins, so every calendar day is fetched exactly once (the final window keeps the
    real end). Callers truncate these bounds to a calendar date (see orchestration._as_date).

    A same CALENDAR-DAY window (end.date() == start.date()) is a valid single-day pull (the inclusive
    endDate means Start == End covers exactly that one day) and returns the single window
    [(start, end)] rather than entering the day-splitting loop below (which requires `cursor < end`
    to make progress). The calendar branch gates on CALENDAR DATES, not full datetimes: callers
    (_compute_window) accept same-day windows purely by calendar date, and `since`/`until` are parsed
    independently (until first, then since — see resolve_window), so a same-day window can have `end`
    a few microseconds before `start` as full timestamps (e.g. Start="today", End="today", or an
    explicit Start=...T18:00, End=...T00:00 on one date) despite covering exactly one valid calendar
    day. Gating on `end < start` (full datetime) would wrongly return [] for that case. The minute
    branch has no such mismatch (it consumes the raw timestamps directly, not a calendar day), so it
    keeps the original full-datetime check: a zero-length minute window has nothing to fetch.
    """
    if max_minutes > 0:
        if end <= start:
            return []
        span = timedelta(minutes=max_minutes)
        windows: list[tuple[datetime, datetime]] = []
        cursor = start
        while cursor < end:
            nxt = min(cursor + span, end)
            windows.append((cursor, nxt))
            cursor = nxt
        return windows
    # Calendar-day chunking with an inclusive endDate: end each non-final window a day early.
    if end.date() < start.date():
        return []
    if end.date() == start.date():
        return [(start, end)]
    span = timedelta(days=max_days)
    windows = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + span, end)
        win_end = nxt if nxt >= end else nxt - timedelta(days=1)
        windows.append((cursor, win_end))
        cursor = nxt
    return windows

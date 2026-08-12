"""Unit tests for Component._compute_window — the fetch-window guard.

A date-windowed resource needs BOTH bounds to build a valid WFM dateRange. `until` defaults to the
run start when empty; `since` has no natural default, so an empty Start Date (or an inverted window)
is rejected up front. Without the guard a half-open window shipped no dateRange and WFM returned its
default period — the "1 row / 3 columns" near-empty result that then failed the output schema check
(the customer-reported "End Date parameter fails").

Component.__new__ skips __init__ (which would build a WfmClient); _compute_window only reads
self._config, which we set directly — same pattern as tests/unit/test_component.py.
"""

import pytest
from keboola.component.exceptions import UserException

from client.resources import get_resource
from component import Component
from configuration import Configuration


def _root():
    return {
        "host": "https://acme.prd.mykronos.com",
        "client_id": "c",
        "#client_secret": "s",
        "username": "u",
        "#password": "p",
    }


def _comp(**params) -> Component:
    comp = Component.__new__(Component)
    comp._config = Configuration(**_root(), **params)
    return comp


def test_empty_start_on_windowed_resource_raises():
    # since empty + End set is the exact customer case: half-open window -> no dateRange -> WFM
    # default period. Must fail fast instead of silently returning near-empty data.
    comp = _comp(resource="timekeeping_timecard_metrics", since="", until="2026-06-01T00:00:00+00:00")
    with pytest.raises(UserException, match="needs a Start Date"):
        comp._compute_window(get_resource("timekeeping_timecard_metrics"))


def test_both_bounds_empty_on_windowed_resource_raises():
    # Both empty -> until defaults to now, since stays None -> still a dropped dateRange. Reject it.
    comp = _comp(resource="timekeeping_timecard_metrics")
    with pytest.raises(UserException, match="needs a Start Date"):
        comp._compute_window(get_resource("timekeeping_timecard_metrics"))


def test_start_set_end_empty_is_valid():
    # The current working shape: Start set, End empty -> [since, now]. Must NOT raise.
    comp = _comp(resource="timekeeping_timecard_metrics", since="2026-01-01T00:00:00+00:00")
    since_iso, until_iso = comp._compute_window(get_resource("timekeeping_timecard_metrics"))
    assert since_iso == "2026-01-01T00:00:00+00:00"
    assert until_iso is not None  # defaulted to run start (now)


def test_both_bounds_set_is_valid():
    comp = _comp(
        resource="timekeeping_timecard_metrics",
        since="2026-01-01T00:00:00+00:00",
        until="2026-06-01T00:00:00+00:00",
    )
    since_iso, until_iso = comp._compute_window(get_resource("timekeeping_timecard_metrics"))
    assert since_iso == "2026-01-01T00:00:00+00:00"
    assert until_iso == "2026-06-01T00:00:00+00:00"


def test_end_on_or_before_start_raises():
    comp = _comp(
        resource="timekeeping_timecard_metrics",
        since="2026-06-01T00:00:00+00:00",
        until="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(UserException, match="must be earlier than End Date"):
        comp._compute_window(get_resource("timekeeping_timecard_metrics"))


def test_symbolic_period_needs_no_start_date():
    # A symbolic period replaces the date window; an empty Start Date is fine and the window is None.
    comp = _comp(resource="timekeeping_timecard_metrics", window_type="symbolic_period", symbolic_period="1")
    assert comp._compute_window(get_resource("timekeeping_timecard_metrics")) == (None, None)


def test_resource_without_date_field_needs_no_start_date():
    # An org-snapshot resource (date_field=None, e.g. persons) has no window and is never gated.
    comp = _comp(resource="persons")
    assert comp._compute_window(get_resource("persons")) == (None, None)


def test_window_days_refused_for_rollup_resource():
    # window_days would split a rollup resource's per-employee aggregate into partial-period rows;
    # run() must refuse it up front (before any API call) and point at batch_size instead.
    comp = _comp(
        resource="timekeeping_timecard_metrics",
        since="2026-01-01T00:00:00+00:00",
        window_days=30,
    )
    with pytest.raises(UserException, match="window_days"):
        comp.run()


def test_empty_metric_group_refused_for_timecard_metrics():
    # An exploded metrics resource needs exactly one section; an empty selection would return ALL
    # sections (not explodable into one table), so run() must fail fast before any API call.
    comp = _comp(resource="timekeeping_timecard_metrics", since="2026-01-01T00:00:00+00:00")
    with pytest.raises(UserException, match="metric group"):
        comp.run()


def test_metric_group_set_passes_the_guard(monkeypatch):
    # With a metric_group set, run() passes the guard and proceeds to fetch (which we stub to no-op).
    comp = _comp(
        resource="timekeeping_timecard_metrics",
        since="2026-01-01T00:00:00+00:00",
        metric_group="ACTUAL_TOTALS",
    )
    monkeypatch.setattr(comp, "_record_source", lambda *a, **k: iter([]))  # short-circuit before HTTP
    # comp bypasses __init__ (see _comp), so it has no data_folder_path; stub the state read the
    # zero-row path takes so the guard is exercised without needing a real datadir.
    monkeypatch.setattr(comp, "get_state_file", lambda: {})
    comp.run()  # must not raise

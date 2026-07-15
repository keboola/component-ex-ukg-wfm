"""Functional replay tests over SYNTHETIC VCR cassettes (no sandbox available).

There is NO public UKG Pro WFM sandbox and NO real credentials for this component, so the
cassettes under ``cassettes/`` are hand-authored to the documented request/response SHAPES
the code actually issues. Every test runs with ``record_mode="none"`` so the network is
never touched: if the component tried to make a request not present in the cassette, VCR
raises rather than recording — which is what keeps the cassettes honest about the real flow.

Coverage (the request flows the production code takes):
  * token -> hyperfind resolve -> single-page multi_read   (date-window resource)
  * token -> hyperfind resolve -> MULTI-PAGE multi_read     (cacheKey/index paging)
  * async payroll submit -> poll (in-progress -> completed) -> download
  * token -> hyperfind resolve -> multi_read                (symbolic_period bound)
  * auth failure (token endpoint 401) -> UserException       (entrypoint maps to exit 1)
"""
import csv
import json
from pathlib import Path

import pytest
import vcr
from keboola.component.exceptions import UserException

my_vcr = vcr.VCR(
    cassette_library_dir=str(Path(__file__).parent / "cassettes"),
    record_mode="none",  # never hit the network; synthetic cassettes only
    match_on=["method", "path"],
    filter_post_data_parameters=["password", "client_secret", "username", "client_id"],
    filter_headers=["authorization"],
)

_AUTH = {
    "host": "https://acme.prd.mykronos.com",
    "#client_id": "cid",
    "#client_secret": "csec",
    "#username": "svc_user",
    "#password": "pw",
}


def _write_datadir(tmp_path, monkeypatch, parameters: dict) -> Path:
    """Lay out a minimal KBC datadir and point KBC_DATADIR at it."""
    data_dir = tmp_path / "data"
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "in").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": parameters}))
    monkeypatch.setenv("KBC_DATADIR", str(data_dir))
    return data_dir


def _read_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="") as fh:
        return list(csv.DictReader(fh))


@my_vcr.use_cassette("timekeeping_punches.yaml")
def test_timekeeping_punches_replay(tmp_path, monkeypatch):
    """Date-window resource: token -> hyperfind -> single-page multi_read."""
    data_dir = _write_datadir(
        tmp_path,
        monkeypatch,
        {
            **_AUTH,
            "resource": "timekeeping_punches",
            "load_type": "incremental_load",
            "since": "30 days ago",
            "hyperfind_ref": "AllHome",
        },
    )

    from component import Component

    Component().run()

    out_csv = data_dir / "out" / "tables" / "timekeeping_punches.csv"
    assert out_csv.exists(), "expected output table was not written"
    rows = _read_rows(out_csv)
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"p1", "p2"}
    assert (data_dir / "out" / "tables" / "timekeeping_punches.csv.manifest").exists()

    # sanity: no stray files leaked into out/tables beyond the resource table + manifest
    written = {p.name for p in (data_dir / "out" / "tables").iterdir()}
    assert written == {"timekeeping_punches.csv", "timekeeping_punches.csv.manifest"}


@my_vcr.use_cassette("timekeeping_punches_paged.yaml")
def test_timekeeping_punches_multipage_replay(tmp_path, monkeypatch):
    """multi_read cacheKey/index paging: page 1 (full) -> page 2 (partial -> stop).

    PAGE_SIZE is shrunk to 2 so a small cassette exercises the real paging branch: the
    first page returns exactly ``count`` records plus a ``cacheKey`` (=> fetch another
    page), the second returns fewer than ``count`` (=> terminate).
    """
    import client.orchestration as orch

    monkeypatch.setattr(orch, "PAGE_SIZE", 2)

    data_dir = _write_datadir(
        tmp_path,
        monkeypatch,
        {
            **_AUTH,
            "resource": "timekeeping_punches",
            "load_type": "incremental_load",
            "since": "30 days ago",
            "hyperfind_ref": "AllHome",
        },
    )

    from component import Component

    Component().run()

    out_csv = data_dir / "out" / "tables" / "timekeeping_punches.csv"
    rows = _read_rows(out_csv)
    assert len(rows) == 3, "both pages should be flattened into the output"
    assert {r["id"] for r in rows} == {"p1", "p2", "p3"}


@my_vcr.use_cassette("payroll_export_async.yaml")
def test_payroll_export_async_replay(tmp_path, monkeypatch):
    """Async export: submit -> poll (IN_PROGRESS -> COMPLETED) -> download the file."""
    # Don't actually sleep between polls.
    import client.payroll as payroll

    monkeypatch.setattr(payroll.time, "sleep", lambda *_: None)

    data_dir = _write_datadir(
        tmp_path,
        monkeypatch,
        {
            **_AUTH,
            "resource": "payroll_export",
            "load_type": "full_load",
            "since": "30 days ago",
            "hyperfind_ref": "AllHome",
            "poll_interval_seconds": 1,
        },
    )

    from component import Component

    Component().run()

    out_csv = data_dir / "out" / "tables" / "payroll_export.csv"
    assert out_csv.exists(), "async export output table was not written"
    rows = _read_rows(out_csv)
    assert len(rows) == 2
    assert {r["employeeId"] for r in rows} == {"101", "102"}


@my_vcr.use_cassette("scheduling_shifts_symbolic.yaml")
def test_scheduling_shifts_symbolic_replay(tmp_path, monkeypatch):
    """symbolic_period bound: token -> hyperfind -> multi_read with a symbolic window."""
    data_dir = _write_datadir(
        tmp_path,
        monkeypatch,
        {
            **_AUTH,
            "resource": "scheduling_shifts",
            "load_type": "full_load",
            "symbolic_period": "Current Pay Period",
            "hyperfind_ref": "AllHome",
        },
    )

    from component import Component

    Component().run()

    out_csv = data_dir / "out" / "tables" / "scheduling_shifts.csv"
    assert out_csv.exists(), "symbolic-period output table was not written"
    rows = _read_rows(out_csv)
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"s1", "s2"}


@my_vcr.use_cassette("auth_failure.yaml")
def test_auth_failure_raises(tmp_path, monkeypatch):
    """Token endpoint returns 401 -> UserException (the entrypoint maps this to exit 1).

    The datadir suite asserts the exit code directly (tests/mock/auth_failure); here we
    assert the exception the __main__ guard catches, so both layers agree on the failure.
    """
    _write_datadir(
        tmp_path,
        monkeypatch,
        {
            **_AUTH,
            "resource": "timekeeping_punches",
            "load_type": "incremental_load",
            "since": "30 days ago",
            "hyperfind_ref": "AllHome",
        },
    )

    from component import Component

    with pytest.raises(UserException) as exc_info:
        Component().run()
    assert "token" in str(exc_info.value).lower()

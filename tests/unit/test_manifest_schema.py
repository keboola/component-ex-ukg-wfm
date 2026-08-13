"""Guard test for the PK-nullability manifest fix.

Keboola Storage rejects a primary key declared on a nullable column, so the
component must emit PK columns as ``nullable: false`` and all other columns as
``nullable: true``. This drives the real ``Component._stream_and_write_table``
code path (no HTTP) and asserts the produced manifest schema.
"""

import json
from pathlib import Path

import pytest

from client.resources import ResourceDef, get_resource
from component import Component

_PARAMS = {
    "host": "https://acme.prd.mykronos.com",
    "client_id": "cid",
    "#client_secret": "csecret",
    "username": "user",
    "#password": "pass",
    "resource": "persons",
    "load_type": "full_load",
}


def _make_datadir(tmp_path: Path, params: dict | None = None) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": params or _PARAMS}))
    return data_dir


@pytest.fixture
def component(tmp_path, monkeypatch) -> Component:
    data_dir = _make_datadir(tmp_path)
    monkeypatch.setenv("KBC_DATADIR", str(data_dir))
    return Component()


def _build_component(tmp_path, monkeypatch, params: dict) -> Component:
    data_dir = _make_datadir(tmp_path, params)
    monkeypatch.setenv("KBC_DATADIR", str(data_dir))
    return Component()


def _manifest(data_dir: Path, table: str) -> dict:
    return json.loads((data_dir / "out" / "tables" / f"{table}.csv.manifest").read_text())


def _schema_by_col(data_dir: Path, table: str) -> dict[str, dict]:
    return {col["name"]: col for col in _manifest(data_dir, table)["schema"]}


def _csv_rows(data_dir: Path, table: str) -> list[str]:
    return (data_dir / "out" / "tables" / f"{table}.csv").read_text().splitlines()


def test_user_primary_key_drives_manifest_and_incremental_on_keyless_resource(tmp_path, monkeypatch):
    """A user-supplied primary_key sets the manifest PK and enables incremental upsert
    on an otherwise-registry-keyless resource."""
    params = {**_PARAMS, "resource": "attestations", "load_type": "incremental_load", "primary_key": ["id"]}
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("attestations")
    assert resource.primary_key == []  # keyless in the registry

    records = iter([{"id": "A1", "value": "x"}])
    row_count, _ = component._stream_and_write_table(resource, records)
    assert row_count == 1

    manifest = _manifest(tmp_path / "data", "attestations")
    # PK enables incremental upsert; the PK is carried per-column in the schema.
    assert manifest["incremental"] is True

    cols = _schema_by_col(tmp_path / "data", "attestations")
    assert cols["id"].get("nullable", False) is False
    assert cols["id"]["primary_key"] is True
    assert cols["value"]["nullable"] is True
    assert cols["value"].get("primary_key", False) is False


def test_keyless_incremental_without_user_pk_stays_full_replace(tmp_path, monkeypatch):
    """Keyless resource + incremental_load + no user PK stays incremental=false (non-breaking)."""
    params = {**_PARAMS, "resource": "attestations", "load_type": "incremental_load"}
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("attestations")

    records = iter([{"id": "A1", "value": "x"}])
    component._stream_and_write_table(resource, records)

    manifest = _manifest(tmp_path / "data", "attestations")
    assert manifest.get("incremental", False) is False

    # No column is flagged as a primary key.
    cols = _schema_by_col(tmp_path / "data", "attestations")
    assert all(col.get("primary_key", False) is False for col in cols.values())


def test_pk_columns_non_nullable_others_nullable(component, tmp_path):
    """A PK resource must emit PK cols non-nullable and non-PK cols nullable."""
    resource = get_resource("persons")
    assert resource.primary_key == ["personNumber"]

    records = iter([{"personNumber": "E1", "firstName": "A", "lastName": "B"}])
    row_count, columns = component._stream_and_write_table(resource, records)
    assert row_count == 1

    cols = _schema_by_col(tmp_path / "data", "persons")

    # PK column: non-nullable (nullable omitted or False), flagged primary_key.
    assert cols["personNumber"].get("nullable", False) is False
    assert cols["personNumber"]["primary_key"] is True

    # Non-PK columns: nullable, not primary_key.
    for name in ("firstName", "lastName"):
        assert cols[name]["nullable"] is True
        assert cols[name].get("primary_key", False) is False


def test_keyless_resource_all_columns_nullable(component, tmp_path):
    """A resource with no primary key emits every column nullable."""
    resource = ResourceDef(
        name="keyless_probe",
        family="people",
        method="GET",
        endpoint_path="/probe",
        primary_key=[],
    )
    records = iter([{"a": "1", "b": "2"}])
    row_count, _ = component._stream_and_write_table(resource, records)
    assert row_count == 1

    cols = _schema_by_col(tmp_path / "data", "keyless_probe")
    for name in ("a", "b"):
        assert cols[name]["nullable"] is True
        assert cols[name].get("primary_key", False) is False


def test_ragged_rows_backfill_later_added_column_as_empty(component, tmp_path):
    """Locks in the phase-2 reorder in _stream_and_write_table (csv.reader + a precomputed
    name->index permutation, replacing DictReader/DictWriter) against the ragged-row case: a
    column that first appears on a LATER row must still read back correctly for every row once
    columns are re-sorted, with the earlier row's missing value backfilled as an empty string —
    the same restval='' semantics as before, just computed without per-row dict construction."""
    resource = ResourceDef(
        name="ragged_probe",
        family="people",
        method="GET",
        endpoint_path="/probe",
        primary_key=[],
    )
    # Row 1 has no "c" — it is only introduced by row 2, so phase 1 writes row 1 with fewer
    # physical columns than row 2 (a growing insertion-order fieldnames list).
    records = iter(
        [
            {"a": "1", "b": "2"},
            {"a": "3", "b": "4", "c": "5"},
        ]
    )
    row_count, columns = component._stream_and_write_table(resource, records)
    assert row_count == 2
    assert columns == ["a", "b", "c"]

    rows = _csv_rows(tmp_path / "data", "ragged_probe")
    assert rows[0] == "a,b,c"
    assert rows[1] == "1,2,"  # row 1's "c" backfilled empty
    assert rows[2] == "3,4,5"


def test_full_load_backfills_sticky_columns_from_state(tmp_path, monkeypatch):
    """A full_load run whose data omits an optional column must still emit it (unioned from state),
    so a native-typed REPLACE isn't rejected with 'Missing columns'. Regression for the downstream
    schema-mismatch failure the End-Date bug produced (near-empty result dropped 'actualTotals').
    Sticky columns now apply to full loads, not just incremental.

    timekeeping_timecard_metrics is exploded: a source record with no line-item section (mirroring
    an employee with nothing to report) now correctly yields ZERO output rows (not a phantom
    identity row), so this run falls through to the zero-row header-only path. The sticky-column
    backfill must still apply there, from state, so the destination schema doesn't narrow."""
    params = {**_PARAMS, "resource": "timekeeping_timecard_metrics", "load_type": "full_load"}
    data_dir = _make_datadir(tmp_path, params)
    # A prior good run recorded the full column set (incl. the optional actualTotals) in state.
    (data_dir / "in" / "state.json").write_text(
        json.dumps(
            {
                "schema_columns": {
                    "timekeeping_timecard_metrics": [
                        "actualTotals",
                        "employeeId_id",
                        "employeeId_name",
                        "employeeId_qualifier",
                    ]
                }
            }
        )
    )
    monkeypatch.setenv("KBC_DATADIR", str(data_dir))
    component = Component()
    resource = get_resource("timekeeping_timecard_metrics")

    # This run mirrors the near-empty response: no record carries a line-item section, so explode
    # drops every record and the run yields zero data rows.
    records = iter([{"employeeId_id": "E1", "employeeId_name": "x", "employeeId_qualifier": "q"}])
    row_count, columns = component._stream_and_write_table(resource, records)

    assert row_count == 0
    assert "actualTotals" in columns  # back-filled from sticky state despite being absent in the data
    cols = _schema_by_col(tmp_path / "data", "timekeeping_timecard_metrics")
    assert {"actualTotals", "employeeId_id", "employeeId_name", "employeeId_qualifier"} <= set(cols)


def test_timecard_metrics_explodes_to_line_items_keyed_on_uniqueid(tmp_path, monkeypatch):
    """Exploded metrics: one row per line item, keyed on uniqueId (non-nullable PK), applyDate
    promoted to a column; an employee with an empty section contributes no rows."""
    params = {
        **_PARAMS,
        "resource": "timekeeping_timecard_metrics",
        "load_type": "incremental_load",
        "metric_group": "ACTUAL_TOTALS",
    }
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("timekeeping_timecard_metrics")

    records = iter(
        [
            {
                "employeeId": {"id": 14212},
                "actualTotals": [
                    {"uniqueId": "14212:2026-07-27:409", "applyDate": "2026-07-27", "hoursAmount": 8.0},
                    {"uniqueId": "14212:2026-07-26:801", "applyDate": "2026-07-26", "hoursAmount": 6.0},
                ],
            },
            {"employeeId": {"id": 67127}, "actualTotals": []},  # empty section -> no rows
        ]
    )
    row_count, columns = component._stream_and_write_table(resource, records)

    assert row_count == 2
    # metric_group="ACTUAL_TOTALS" -> the exploded output table is named per metric group.
    manifest = _manifest(tmp_path / "data", "timekeeping_timecard_metrics_actual_totals")
    assert manifest["incremental"] is True
    cols = _schema_by_col(tmp_path / "data", "timekeeping_timecard_metrics_actual_totals")
    assert cols["uniqueId"]["primary_key"] is True
    assert cols["uniqueId"].get("nullable", False) is False
    assert {"uniqueId", "employeeId_id", "applyDate", "hoursAmount"} <= set(cols)


def test_zero_row_full_load_warns_about_replacing_destination(tmp_path, monkeypatch, caplog):
    """A zero-row full load replaces the destination table with an empty header-only one — a
    transient empty API response would silently truncate previously loaded data. It must warn."""
    params = {**_PARAMS, "resource": "persons", "load_type": "full_load"}
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("persons")

    with caplog.at_level("WARNING"):
        row_count, columns = component._write_empty_table(resource)

    assert row_count == 0
    assert columns  # known columns (the primary key) -> the header-only path, not the no-schema path
    assert any("persons" in record.message and "full load" in record.message.lower() for record in caplog.records)


def test_zero_row_incremental_load_does_not_warn(tmp_path, monkeypatch, caplog):
    """A zero-row incremental run is a no-op append, not a truncation — it must not warn."""
    params = {**_PARAMS, "resource": "persons", "load_type": "incremental_load"}
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("persons")

    with caplog.at_level("WARNING"):
        row_count, columns = component._write_empty_table(resource)

    assert row_count == 0
    assert columns
    assert not any(record.levelname == "WARNING" for record in caplog.records)


def test_user_primary_key_overridden_by_unique_id_warns(tmp_path, monkeypatch, caplog):
    """An exploded resource whose rows carry `uniqueId` always keys on it, even when the user
    configured a different primary_key. That override is correct but silent otherwise — it must
    warn so the user understands why their configured PK was not used."""
    params = {
        **_PARAMS,
        "resource": "timekeeping_timecard_metrics",
        "load_type": "incremental_load",
        "metric_group": "ACTUAL_TOTALS",
        "primary_key": ["employeeId_id"],
    }
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("timekeeping_timecard_metrics")

    records = iter(
        [
            {
                "employeeId": {"id": 14212},
                "actualTotals": [
                    {"uniqueId": "14212:2026-07-27:409", "applyDate": "2026-07-27", "hoursAmount": 8.0},
                ],
            }
        ]
    )
    with caplog.at_level("WARNING"):
        row_count, _ = component._stream_and_write_table(resource, records)

    assert row_count == 1
    cols = _schema_by_col(tmp_path / "data", "timekeeping_timecard_metrics_actual_totals")
    assert cols["uniqueId"]["primary_key"] is True
    assert cols["employeeId_id"].get("primary_key", False) is False
    assert any(
        "employeeId_id" in record.message and "uniqueId" in record.message and record.levelname == "WARNING"
        for record in caplog.records
    )


def test_no_user_primary_key_on_exploded_resource_does_not_warn_about_override(tmp_path, monkeypatch, caplog):
    """No override warning when the user never set a primary_key in the first place — uniqueId is
    simply the resource's natural key, nothing was overridden."""
    params = {
        **_PARAMS,
        "resource": "timekeeping_timecard_metrics",
        "load_type": "incremental_load",
        "metric_group": "ACTUAL_TOTALS",
    }
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("timekeeping_timecard_metrics")

    records = iter(
        [
            {
                "employeeId": {"id": 14212},
                "actualTotals": [
                    {"uniqueId": "14212:2026-07-27:409", "applyDate": "2026-07-27", "hoursAmount": 8.0},
                ],
            }
        ]
    )
    with caplog.at_level("WARNING"):
        component._stream_and_write_table(resource, records)

    assert not any("overridden" in record.message for record in caplog.records)


def test_exploded_resource_without_unique_id_and_no_user_pk_warns_keyless_full_replace(tmp_path, monkeypatch, caplog):
    """A metric group section without `uniqueId` (and no user-supplied primary_key) has no
    incremental key at all: it silently falls back to a keyless full replace. That must be
    surfaced, since an incremental config would otherwise appear to do nothing."""
    params = {
        **_PARAMS,
        "resource": "timekeeping_timecard_metrics",
        "load_type": "incremental_load",
        "metric_group": "SCHEDULED_TOTALS",
    }
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("timekeeping_timecard_metrics")

    # This section's line items carry no uniqueId — an exploded row with no natural key.
    records = iter(
        [
            {
                "employeeId": {"id": 14212},
                "scheduledTotals": [
                    {"applyDate": "2026-07-27", "hoursAmount": 8.0},
                ],
            }
        ]
    )
    with caplog.at_level("WARNING"):
        row_count, columns = component._stream_and_write_table(resource, records)

    assert row_count == 1
    assert "uniqueId" not in columns
    manifest = _manifest(tmp_path / "data", "timekeeping_timecard_metrics_scheduled_totals")
    assert manifest.get("incremental", False) is False
    cols = _schema_by_col(tmp_path / "data", "timekeeping_timecard_metrics_scheduled_totals")
    assert all(col.get("primary_key", False) is False for col in cols.values())
    assert any(
        "timekeeping_timecard_metrics" in record.message
        and "no incremental key" in record.message
        and "full replace" in record.message
        and record.levelname == "WARNING"
        for record in caplog.records
    )


def test_exploded_resource_with_unique_id_does_not_warn_keyless_full_replace(tmp_path, monkeypatch, caplog):
    """When the exploded rows DO carry uniqueId, there is a real incremental key, so the
    keyless-full-replace warning must not fire."""
    params = {
        **_PARAMS,
        "resource": "timekeeping_timecard_metrics",
        "load_type": "incremental_load",
        "metric_group": "ACTUAL_TOTALS",
    }
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("timekeeping_timecard_metrics")

    records = iter(
        [
            {
                "employeeId": {"id": 14212},
                "actualTotals": [
                    {"uniqueId": "14212:2026-07-27:409", "applyDate": "2026-07-27", "hoursAmount": 8.0},
                ],
            }
        ]
    )
    with caplog.at_level("WARNING"):
        component._stream_and_write_table(resource, records)

    assert not any("full replace" in record.message for record in caplog.records)


def test_exploded_resource_without_unique_id_but_with_user_pk_does_not_warn_keyless(tmp_path, monkeypatch, caplog):
    """A user-supplied primary_key gives a section without uniqueId a real incremental key, so the
    keyless-full-replace warning must not fire (there IS a usable key, just not uniqueId)."""
    params = {
        **_PARAMS,
        "resource": "timekeeping_timecard_metrics",
        "load_type": "incremental_load",
        "metric_group": "SCHEDULED_TOTALS",
        "primary_key": ["employeeId_id"],
    }
    component = _build_component(tmp_path, monkeypatch, params)
    resource = get_resource("timekeeping_timecard_metrics")

    records = iter(
        [
            {
                "employeeId": {"id": 14212},
                "scheduledTotals": [
                    {"applyDate": "2026-07-27", "hoursAmount": 8.0},
                ],
            }
        ]
    )
    with caplog.at_level("WARNING"):
        row_count, _ = component._stream_and_write_table(resource, records)

    assert row_count == 1
    cols = _schema_by_col(tmp_path / "data", "timekeeping_timecard_metrics_scheduled_totals")
    assert cols["employeeId_id"]["primary_key"] is True
    assert not any("full replace" in record.message for record in caplog.records)


def test_composite_pk_all_key_columns_non_nullable(component, tmp_path):
    """Every column in a composite primary key must be non-nullable."""
    resource = ResourceDef(
        name="composite_probe",
        family="people",
        method="GET",
        endpoint_path="/probe",
        primary_key=["k1", "k2"],
    )
    records = iter([{"k1": "1", "k2": "2", "v": "x"}])
    component._stream_and_write_table(resource, records)

    cols = _schema_by_col(tmp_path / "data", "composite_probe")
    assert cols["k1"].get("nullable", False) is False
    assert cols["k2"].get("nullable", False) is False
    assert cols["k1"]["primary_key"] is True
    assert cols["k2"]["primary_key"] is True
    assert cols["v"]["nullable"] is True

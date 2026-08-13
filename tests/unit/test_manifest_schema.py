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


def test_full_load_backfills_sticky_columns_from_state(tmp_path, monkeypatch):
    """A full_load run whose data omits an optional column must still emit it (unioned from state),
    so a native-typed REPLACE isn't rejected with 'Missing columns'. Regression for the downstream
    schema-mismatch failure the End-Date bug produced (near-empty result dropped 'actualTotals').
    Sticky columns now apply to full loads, not just incremental."""
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

    # This run mirrors the near-empty response: the actualTotals column is absent from the data.
    records = iter([{"employeeId_id": "E1", "employeeId_name": "x", "employeeId_qualifier": "q"}])
    row_count, columns = component._stream_and_write_table(resource, records)

    assert row_count == 1
    assert "actualTotals" in columns  # back-filled from sticky state despite being absent in the data
    cols = _schema_by_col(tmp_path / "data", "timekeeping_timecard_metrics")
    assert {"actualTotals", "employeeId_id", "employeeId_name", "employeeId_qualifier"} <= set(cols)


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

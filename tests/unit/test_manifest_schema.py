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
    "#client_id": "cid",
    "#client_secret": "csecret",
    "#username": "user",
    "#password": "pass",
    "resource": "persons",
    "load_type": "full_load",
}


def _make_datadir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": _PARAMS}))
    return data_dir


@pytest.fixture
def component(tmp_path, monkeypatch) -> Component:
    data_dir = _make_datadir(tmp_path)
    monkeypatch.setenv("KBC_DATADIR", str(data_dir))
    return Component()


def _schema_by_col(data_dir: Path, table: str) -> dict[str, dict]:
    manifest = json.loads((data_dir / "out" / "tables" / f"{table}.csv.manifest").read_text())
    return {col["name"]: col for col in manifest["schema"]}


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

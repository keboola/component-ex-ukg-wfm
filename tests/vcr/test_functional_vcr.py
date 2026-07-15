"""Functional replay test over SYNTHETIC VCR cassettes (no sandbox available).

Replays the token -> hyperfind -> multi_read happy path for timekeeping_punches end-to-end
through Component().run(), asserting the output CSV is produced. record_mode="none" guarantees
no network is ever touched — cassettes are hand-authored to documented WFM shapes.
"""
import csv
import json
import os
from pathlib import Path

import vcr

my_vcr = vcr.VCR(
    cassette_library_dir=str(Path(__file__).parent / "cassettes"),
    record_mode="none",  # never hit the network; synthetic cassettes only
    match_on=["method", "path"],
    filter_post_data_parameters=["password", "client_secret", "username", "client_id"],
    filter_headers=["authorization"],
)

_CONFIG = {
    "parameters": {
        "host": "https://acme.prd.mykronos.com",
        "#client_id": "cid",
        "#client_secret": "csec",
        "#username": "svc_user",
        "#password": "pw",
        "resource": "timekeeping_punches",
        "load_type": "incremental_load",
        "since": "30 days ago",
        "hyperfind_ref": "AllHome",
    }
}


@my_vcr.use_cassette("timekeeping_punches.yaml")
def test_timekeeping_punches_replay(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "in").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps(_CONFIG))
    monkeypatch.setenv("KBC_DATADIR", str(data_dir))

    from component import Component

    Component().run()

    out_csv = data_dir / "out" / "tables" / "timekeeping_punches.csv"
    assert out_csv.exists(), "expected output table was not written"
    with open(out_csv, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    ids = {r["id"] for r in rows}
    assert ids == {"p1", "p2"}
    # manifest emitted alongside the CSV
    assert (data_dir / "out" / "tables" / "timekeeping_punches.csv.manifest").exists()

    # sanity: no stray files leaked into out/tables beyond the resource table + manifest
    written = {p.name for p in (data_dir / "out" / "tables").iterdir()}
    assert written == {"timekeeping_punches.csv", "timekeeping_punches.csv.manifest"}
    assert os.environ["KBC_DATADIR"] == str(data_dir)

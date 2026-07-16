import requests_mock

from client.orchestration import chunk_and_read, iter_records, paginate_multi_read, resolve_employee_ids
from client.resources import get_resource
from client.wfm_client import WfmClient

HOST = "https://acme.prd.mykronos.com"
AUTH_URL = f"{HOST}/api/authentication/access_token"


def _client(m):
    m.post(AUTH_URL, json={"access_token": "T", "refresh_token": "R", "expires_in": 3600})
    return WfmClient(HOST, "c", "s", "u", "p")


def test_resolve_employee_ids_from_hyperfind():
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(f"{HOST}/api/v1/commons/hyperfind/execute", json={"result": [{"id": 11}, {"id": 22}]})
        assert resolve_employee_ids(c, "MyQuery") == [11, 22]


def test_hyperfind_empty_result_short_circuits_without_unscoped_read():
    # An empty Hyperfind must NOT fall through to a multi_read with no employee filter
    # (WFM would read all employees). iter_records should yield nothing and never POST.
    res = get_resource("timekeeping_punches")  # employee_scope == HYPERFIND
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(f"{HOST}/api/v1/commons/hyperfind/execute", json={"result": []})
        read = m.post(f"{HOST}/api/v1{res.endpoint_path}", json={"records": [{"id": 1}]})
        rows = list(
            iter_records(
                c,
                res,
                hyperfind_ref="Empty",
                since_iso="2026-01-01T00:00:00+00:00",
                until_iso="2026-02-01T00:00:00+00:00",
                select=[],
            )
        )
        assert rows == []
        assert read.call_count == 0  # no unscoped read issued


def test_paginate_multi_read_follows_cachekey():
    res = get_resource("timekeeping_punches")
    with requests_mock.Mocker() as m:
        c = _client(m)
        url = f"{HOST}/api/v1{res.endpoint_path}"
        m.post(
            url,
            [
                {"json": {"records": [{"id": 1}, {"id": 2}], "cacheKey": "K", "count": 2}},
                {"json": {"records": [{"id": 3}], "cacheKey": "K", "count": 2}},
            ],
        )
        rows = list(paginate_multi_read(c, res, {"select": [], "count": 2}))
        assert [r["id"] for r in rows] == [1, 2, 3]


def test_chunk_and_read_shrinks_on_413():
    res = get_resource("work_activity_net_changes")  # batch_limit 50
    with requests_mock.Mocker() as m:
        c = _client(m)
        url = f"{HOST}/api/v1{res.endpoint_path}"
        # First (large) chunk 413s; after shrink, chunks succeed with one record each.
        responses = [
            {"status_code": 413, "json": {}},
            {"status_code": 200, "json": {"records": [{"id": 1}]}},
            {"status_code": 200, "json": {"records": [{"id": 2}]}},
        ]
        m.post(url, responses)
        rows = list(chunk_and_read(c, res, list(range(50)), "2026-01-01", "2026-02-01", []))
        assert {r["id"] for r in rows} == {1, 2}


def test_net_change_runs_as_date_window_without_token():
    """I1: keyless net_change is a full refresh -- it must send a dateRange window and
    must NOT send a netChangeToken (the inert token path is removed)."""
    res = get_resource("work_activity_net_changes")
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(f"{HOST}/api/v1/commons/hyperfind/execute", json={"result": [{"id": 7}]})

        def _capture(request, context):
            captured.append(request.json())
            return {"records": [{"id": 1}]}

        m.post(f"{HOST}/api/v1{res.endpoint_path}", json=_capture)
        rows = list(
            iter_records(
                c,
                res,
                hyperfind_ref="AllHome",
                since_iso="2026-01-01T00:00:00+00:00",
                until_iso="2026-02-01T00:00:00+00:00",
                select=[],
            )
        )
    assert [r["id"] for r in rows] == [1]
    assert captured, "net_change resource issued no multi_read call"
    body = captured[0]
    assert "netChangeToken" not in body
    assert body["where"]["dateRange"]["startDate"] == "2026-01-01T00:00:00+00:00"


def test_symbolic_period_replaces_date_window():
    """I2: when a symbolic period is set, the request sends where.symbolicPeriod and
    omits the date window entirely (window/watermark logic is skipped upstream)."""
    res = get_resource("scheduling_shifts")
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(f"{HOST}/api/v1/commons/hyperfind/execute", json={"result": [{"id": 7}]})

        def _capture(request, context):
            captured.append(request.json())
            return {"records": [{"id": 1}]}

        m.post(f"{HOST}/api/v1{res.endpoint_path}", json=_capture)
        rows = list(
            iter_records(
                c,
                res,
                hyperfind_ref="AllHome",
                since_iso=None,
                until_iso=None,
                select=[],
                symbolic_period="Current Pay Period",
            )
        )
    assert [r["id"] for r in rows] == [1]
    body = captured[0]
    assert body["where"]["symbolicPeriod"] == {"qualifier": "Current Pay Period"}
    assert "dateRange" not in body["where"]

import requests_mock

from client.orchestration import chunk_and_read, paginate_multi_read, resolve_employee_ids
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
        m.post(f"{HOST}/api/v1/commons/hyperfind/execute",
               json={"result": [{"id": 11}, {"id": 22}]})
        assert resolve_employee_ids(c, "MyQuery") == [11, 22]


def test_paginate_multi_read_follows_cachekey():
    res = get_resource("timekeeping_punches")
    with requests_mock.Mocker() as m:
        c = _client(m)
        url = f"{HOST}/api/v1{res.endpoint_path}"
        m.post(url, [
            {"json": {"records": [{"id": 1}, {"id": 2}], "cacheKey": "K", "count": 2}},
            {"json": {"records": [{"id": 3}], "cacheKey": "K", "count": 2}},
        ])
        rows = list(paginate_multi_read(c, res, {"select": [], "count": 2}))
        assert [r["id"] for r in rows] == [1, 2, 3]


def test_chunk_and_read_shrinks_on_413():
    res = get_resource("work_activity_net_changes")  # batch_limit 50
    with requests_mock.Mocker() as m:
        c = _client(m)
        url = f"{HOST}/api/v1{res.endpoint_path}"
        # First (large) chunk 413s; after shrink, chunks succeed with one record each.
        responses = [{"status_code": 413, "json": {}},
                     {"status_code": 200, "json": {"records": [{"id": 1}]}},
                     {"status_code": 200, "json": {"records": [{"id": 2}]}}]
        m.post(url, responses)
        rows = list(chunk_and_read(c, res, list(range(50)), "2026-01-01", "2026-02-01", []))
        assert {r["id"] for r in rows} == {1, 2}

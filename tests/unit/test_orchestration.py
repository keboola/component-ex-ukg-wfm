import pytest
import requests_mock
from keboola.component.exceptions import UserException

from client.orchestration import chunk_and_read, iter_records, paginate_multi_read, resolve_employee_ids
from client.resources import get_resource
from client.wfm_client import WfmClient

HOST = "https://acme.prd.mykronos.com"
AUTH_URL = f"{HOST}/api/authentication/access_token"


def _client(m):
    m.post(AUTH_URL, json={"access_token": "T", "refresh_token": "R", "expires_in": 3600})
    return WfmClient(HOST, "c", "s", "u", "p")


def test_resolve_employee_ids_from_hyperfind():
    # Real API shape: {"count", "result": {"refs": [{id,qualifier}], "basePersons": [...]}}.
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)

        def _capture(request, context):
            captured.append(request.json())
            return {"count": 2, "result": {"refs": [{"id": 11}, {"id": 22}], "basePersons": []}}

        m.post(f"{HOST}/api/v1/commons/hyperfind/execute", json=_capture)
        assert resolve_employee_ids(c, "253", "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00") == [11, 22]
    # A numeric ref is sent as hyperfind.id (int) and execute always carries a calendar dateRange.
    body = captured[0]
    assert body["hyperfind"] == {"id": 253}
    assert body["dateRange"] == {"startDate": "2026-01-01", "endDate": "2026-02-01"}
    # A high default threshold is always sent, else a broad Hyperfind 400s with WCO-112003.
    assert body["threshold"] == 50000


def test_resolve_employee_ids_forwards_custom_threshold():
    # A caller-supplied threshold overrides the default and is sent verbatim in the body.
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)

        def _capture(request, context):
            captured.append(request.json())
            return {"count": 0, "result": {"refs": [], "basePersons": []}}

        m.post(f"{HOST}/api/v1/commons/hyperfind/execute", json=_capture)
        resolve_employee_ids(
            c, "All People", "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00", threshold=100000
        )
    assert captured[0]["threshold"] == 100000


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


def test_paginate_multi_read_single_request_no_cachekey():
    # multi_read reads return the whole batch in one response and reject cacheKey/count paging props;
    # paginate_multi_read must issue exactly one request per (already-batched) body.
    res = get_resource("timekeeping_timecards")  # PaginationStyle.NONE (single request per chunk)
    with requests_mock.Mocker() as m:
        c = _client(m)
        read = m.post(f"{HOST}/api/v1{res.endpoint_path}", json={"records": [{"id": 1}, {"id": 2}]})
        rows = list(paginate_multi_read(c, res, {"where": {"employees": {"ids": [1]}}}))
        assert [r["id"] for r in rows] == [1, 2]
        assert read.call_count == 1


def test_apply_read_pages_via_count_index():
    # persons apply_read pages by count+index in the body; loop stops when a page returns < count.
    res = get_resource("persons")  # PaginationStyle.APPLY_READ, page_count=1000
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)

        def _capture(request, context):
            body = request.json()
            captured.append(body)
            # First full page (== count) forces a second request; second page is short -> stop.
            if body["index"] == 0:
                return {"totalElements": 1001, "records": [{"personNumber": str(i)} for i in range(1000)]}
            return {"totalElements": 1001, "records": [{"personNumber": "x"}]}

        m.post(f"{HOST}/api/v1{res.endpoint_path}", json=_capture)
        rows = list(paginate_multi_read(c, res, {"where": {}}))
        assert len(rows) == 1001
        assert [b["index"] for b in captured] == [0, 1]
        assert captured[0]["count"] == 1000


def test_apply_read_page_size_overrides_count():
    # page_size overrides resource.page_count as the per-page `count`; a single short page stops.
    res = get_resource("persons")  # PaginationStyle.APPLY_READ, page_count=1000
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)

        def _capture(request, context):
            body = request.json()
            captured.append(body)
            # Return fewer than page_size (25) so the loop stops after one page.
            return {"records": [{"personNumber": str(i)} for i in range(10)]}

        m.post(f"{HOST}/api/v1{res.endpoint_path}", json=_capture)
        rows = list(paginate_multi_read(c, res, {"where": {}}, page_size=25))
        assert len(rows) == 10
        assert len(captured) == 1
        assert captured[0]["count"] == 25  # page_size overrode resource.page_count (1000)


def test_apply_read_max_pages_caps_loop():
    # max_pages caps the apply_read page loop even when every page is full (never short).
    res = get_resource("persons")
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)

        def _capture(request, context):
            captured.append(request.json())
            # Always a FULL page (== count) so the loop would run forever without the cap.
            return {"records": [{"personNumber": str(i)} for i in range(5)]}

        m.post(f"{HOST}/api/v1{res.endpoint_path}", json=_capture)
        rows = list(paginate_multi_read(c, res, {"where": {}}, page_size=5, max_pages=2))
        assert len(rows) == 10  # exactly 2 pages x 5 rows
        assert [b["index"] for b in captured] == [0, 1]  # capped at 2 pages


def test_apply_read_punches_body_shape():
    # punches apply_read: where.employees.ids + where.dateRange with DATETIME keys, count 1..25.
    res = get_resource("timekeeping_punches")
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(
            f"{HOST}/api/v1/commons/hyperfind/execute",
            json={"count": 1, "result": {"refs": [{"id": 7}], "basePersons": []}},
        )

        def _capture(request, context):
            captured.append(request.json())
            return {"metadata": {}, "data": []}

        m.post(f"{HOST}/api/v1{res.endpoint_path}", json=_capture)
        list(
            iter_records(
                c,
                res,
                hyperfind_ref="AllHome",
                since_iso="2026-07-15T08:00:00+00:00",
                until_iso="2026-07-15T08:30:00+00:00",
                select=[],
            )
        )
    assert captured, "punches apply_read issued no call"
    body = captured[0]
    assert body["where"]["employees"] == {"ids": [7]}
    assert body["where"]["dateRange"] == {"startDateTime": "2026-07-15T08:00:00", "endDateTime": "2026-07-15T08:30:00"}
    assert body["count"] == 25 and body["index"] == 0


def test_paginate_multi_read_honors_records_key_envelope():
    # scheduling_shifts surfaces the "shifts" envelope from the composite schedule response.
    res = get_resource("scheduling_shifts")
    assert res.records_key == "shifts"
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(
            f"{HOST}/api/v1{res.endpoint_path}",
            json={"shifts": [{"id": 9}], "scheduleDayList": [{"day": "x"}], "employees": [{"id": 1}]},
        )
        rows = list(paginate_multi_read(c, res, {}))
        assert [r["id"] for r in rows] == [9]


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
    """I1: keyless net_change is a full refresh -- it must send a lastRunDateTime/endDateTime
    window and must NOT send a netChangeToken (the inert token path is removed)."""
    res = get_resource("work_activity_net_changes")
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(
            f"{HOST}/api/v1/commons/hyperfind/execute",
            json={"count": 1, "result": {"refs": [{"id": 7}], "basePersons": []}},
        )

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
    # VERIFIED shape: where.employees.ids + lastRunDateTime/endDateTime (DATETIME), select "SEGMENTS".
    assert body["where"]["employees"] == {"ids": [7]}
    assert body["where"]["lastRunDateTime"] == "2026-01-01T00:00:00"
    assert body["where"]["endDateTime"] == "2026-02-01T00:00:00"
    assert body["select"] == "SEGMENTS"


def test_symbolic_period_sent_as_daterange_id():
    """When a symbolic period is set, the request nests it inside dateRange by numeric id
    (WFM rejects a qualifier string with WTK-147500) and omits any start/end date window."""
    # timekeeping_timecards uses the default BodyStyle.WHERE_EMPLOYEES_IDS (where.dateRange).
    res = get_resource("timekeeping_timecards")
    captured: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(
            f"{HOST}/api/v1/commons/hyperfind/execute",
            json={"count": 1, "result": {"refs": [{"id": 7}], "basePersons": []}},
        )

        def _capture(request, context):
            captured.append(request.json())
            return {"records": [{"id": 1}]}

        m.post(f"{HOST}/api/v1{res.endpoint_path}", json=_capture)
        list(
            iter_records(
                c,
                res,
                hyperfind_ref="AllHome",
                since_iso=None,
                until_iso=None,
                select=[],
                symbolic_period="1",
            )
        )
    where = captured[0]["where"]
    assert where["dateRange"] == {"symbolicPeriod": {"id": 1}}
    assert "symbolicPeriod" not in where and "startDate" not in where


def test_symbolic_period_non_numeric_raises():
    """A qualifier name (not an id) is rejected up front with a clear UserException."""
    res = get_resource("timekeeping_timecards")
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(
            f"{HOST}/api/v1/commons/hyperfind/execute",
            json={"count": 1, "result": {"refs": [{"id": 7}], "basePersons": []}},
        )
        m.post(f"{HOST}/api/v1{res.endpoint_path}", json={"records": []})
        with pytest.raises(UserException, match="numeric symbolic-period id"):
            list(
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


def test_scheduling_symbolic_period_unsupported():
    """schedule/multi_read rejects a symbolicPeriod, so the component fails with a clear message."""
    res = get_resource("scheduling_shifts")  # BodyStyle.WHERE_EMPLOYEE_REFS
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(
            f"{HOST}/api/v1/commons/hyperfind/execute",
            json={"count": 1, "result": {"refs": [{"id": 7}], "basePersons": []}},
        )
        m.post(f"{HOST}/api/v1{res.endpoint_path}", json={})
        with pytest.raises(UserException, match="does not support a symbolic period"):
            list(
                iter_records(
                    c,
                    res,
                    hyperfind_ref="AllHome",
                    since_iso=None,
                    until_iso=None,
                    select=[],
                    symbolic_period="4",
                )
            )


def test_batch_size_overrides_resource_batch_limit():
    # A config batch_size splits the roster into smaller per-request chunks (memory control),
    # overriding the resource registry default so each multi_read response stays bounded.
    res = get_resource("timekeeping_timecard_metrics")  # EMPLOYEE_SET_METRICS
    calls: list[dict] = []
    with requests_mock.Mocker() as m:
        c = _client(m)

        def _capture(request, context):
            calls.append(request.json())
            return []

        m.post(f"{HOST}/api/v1{res.endpoint_path}", json=_capture)
        list(chunk_and_read(c, res, [1, 2, 3, 4, 5], "2026-01-01", "2026-01-02", [], batch_size=2))
    # 5 employees at batch_size 2 -> 3 requests of 2, 2, 1 employees.
    sizes = [len(c["where"]["employeeSet"]["employees"]["ids"]) for c in calls]
    assert sizes == [2, 2, 1]


def test_timecard_metrics_default_batch_limit_is_memory_safe():
    # Regression: the all-sections metrics endpoint must not default to the 500-employee batch that
    # OOMed the 256 MB component; a smaller default keeps one parsed batch response in budget.
    assert get_resource("timekeeping_timecard_metrics").batch_limit == 100

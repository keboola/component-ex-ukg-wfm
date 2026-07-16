import pytest
import requests_mock
from keboola.component.exceptions import UserException

from client.payroll import run_async_export
from client.resources import get_resource
from client.wfm_client import WfmClient

HOST = "https://acme.prd.mykronos.com"
AUTH_URL = f"{HOST}/api/authentication/access_token"
ASYNC_URL = f"{HOST}/api/v1/commons/payroll/export/async"
RESPONSE_URL = f"{ASYNC_URL}/EK1/response"
QUERY = "SELECT personnumber FROM payrolldetail"


def _client(m):
    m.post(AUTH_URL, json={"access_token": "T", "refresh_token": "R", "expires_in": 3600})
    return WfmClient(HOST, "c", "s", "u", "p")


def _run(c, **kwargs):
    res = get_resource("payroll_export")
    return list(
        run_async_export(
            c, res, "2026-01-01", "2026-02-01", None, query=QUERY, poll_interval_s=0, sleep=lambda *_: None, **kwargs
        )
    )


def test_async_export_submits_polls_then_downloads_csv():
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(ASYNC_URL, status_code=202, json={"executionKey": "EK1", "state": "IN_PROGRESS"})
        m.get(
            ASYNC_URL,
            [
                {"json": {"records": [{"executionKey": "EK1", "state": "IN_PROGRESS"}]}},
                {"json": {"records": [{"executionKey": "EK1", "state": "SUCCESSFUL"}]}},
            ],
        )
        m.get(RESPONSE_URL, text="id,gross\n1,100\n2,200\n", headers={"Content-Type": "text/csv"})
        rows = _run(c)
        assert rows == [{"id": "1", "gross": "100"}, {"id": "2", "gross": "200"}]


def test_async_export_skips_poll_when_submit_already_successful():
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(ASYNC_URL, status_code=202, json={"executionKey": "EK1", "state": "SUCCESSFUL"})
        m.get(RESPONSE_URL, text="id\n1\n", headers={"Content-Type": "text/csv"})
        assert _run(c) == [{"id": "1"}]


def test_async_export_requires_query():
    with requests_mock.Mocker() as m:
        c = _client(m)
        res = get_resource("payroll_export")
        with pytest.raises(UserException):
            list(run_async_export(c, res, "2026-01-01", "2026-02-01", None, query=None, sleep=lambda *_: None))


def test_async_export_raises_when_submit_missing_execution_key():
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(ASYNC_URL, status_code=202, json={"state": "IN_PROGRESS"})
        with pytest.raises(UserException):
            _run(c)


def test_async_export_raises_on_max_wait():
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(ASYNC_URL, status_code=202, json={"executionKey": "EK1", "state": "IN_PROGRESS"})
        m.get(ASYNC_URL, json={"records": [{"executionKey": "EK1", "state": "IN_PROGRESS"}]})
        with pytest.raises(UserException):
            _run(c, max_wait_s=0)


def test_async_export_raises_on_failed_state():
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(ASYNC_URL, status_code=202, json={"executionKey": "EK1", "state": "IN_PROGRESS"})
        m.get(ASYNC_URL, json={"records": [{"executionKey": "EK1", "state": "FAILED"}]})
        with pytest.raises(UserException):
            _run(c)


def test_async_export_raises_user_exception_on_download_failure():
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(ASYNC_URL, status_code=202, json={"executionKey": "EK1", "state": "SUCCESSFUL"})
        m.get(RESPONSE_URL, status_code=500, text="boom")
        with pytest.raises(UserException):
            _run(c)

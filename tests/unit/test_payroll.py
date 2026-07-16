import pytest
import requests_mock
from keboola.component.exceptions import UserException

from client.payroll import run_async_export
from client.resources import get_resource
from client.wfm_client import WfmClient

HOST = "https://acme.prd.mykronos.com"
AUTH_URL = f"{HOST}/api/authentication/access_token"


def _client(m):
    m.post(AUTH_URL, json={"access_token": "T", "refresh_token": "R", "expires_in": 3600})
    return WfmClient(HOST, "c", "s", "u", "p")


def test_async_export_polls_then_downloads():
    res = get_resource("payroll_export")
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(f"{HOST}/api/v1/payroll/export", json={"id": "J1"})
        m.get(
            f"{HOST}/api/v1/payroll/export/J1/status",
            [
                {"json": {"status": "IN_PROGRESS"}},
                {"json": {"status": "COMPLETED", "downloadUrl": f"{HOST}/api/v1/payroll/export/J1/file"}},
            ],
        )
        m.get(f"{HOST}/api/v1/payroll/export/J1/file", json={"records": [{"id": 1}, {"id": 2}]})
        rows = list(
            run_async_export(c, res, "2026-01-01", "2026-02-01", None, poll_interval_s=0, sleep=lambda *_: None)
        )
        assert [r["id"] for r in rows] == [1, 2]


def test_async_export_raises_on_max_wait():
    res = get_resource("payroll_export")
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(f"{HOST}/api/v1/payroll/export", json={"id": "J1"})
        m.get(f"{HOST}/api/v1/payroll/export/J1/status", json={"status": "IN_PROGRESS"})
        with pytest.raises(UserException):
            list(
                run_async_export(
                    c, res, "2026-01-01", "2026-02-01", None, max_wait_s=0, poll_interval_s=0, sleep=lambda *_: None
                )
            )


def test_async_export_raises_user_exception_on_download_failure():
    res = get_resource("payroll_export")
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(f"{HOST}/api/v1/payroll/export", json={"id": "J1"})
        m.get(
            f"{HOST}/api/v1/payroll/export/J1/status",
            json={"status": "COMPLETED", "downloadUrl": f"{HOST}/api/v1/payroll/export/J1/file"},
        )
        m.get(f"{HOST}/api/v1/payroll/export/J1/file", status_code=500, json={"error": "boom"})
        with pytest.raises(UserException):
            list(run_async_export(c, res, "2026-01-01", "2026-02-01", None, poll_interval_s=0, sleep=lambda *_: None))


def test_async_export_raises_on_failed_status():
    res = get_resource("payroll_export")
    with requests_mock.Mocker() as m:
        c = _client(m)
        m.post(f"{HOST}/api/v1/payroll/export", json={"id": "J1"})
        m.get(f"{HOST}/api/v1/payroll/export/J1/status", json={"status": "FAILED"})
        with pytest.raises(UserException):
            list(run_async_export(c, res, "2026-01-01", "2026-02-01", None, poll_interval_s=0, sleep=lambda *_: None))

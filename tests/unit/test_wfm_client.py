import time

import pytest
import requests
import requests_mock
from keboola.component.exceptions import UserException

from client.wfm_client import PayloadTooLargeError, WfmClient

HOST = "https://acme.prd.mykronos.com"
AUTH_URL = f"{HOST}/api/authentication/access_token"


def _client():
    return WfmClient(HOST, "cid", "csecret", "svc_user", "pw")


def test_get_token_uses_password_grant():
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        c = _client()
        assert c.get_token() == "T1"
        sent = m.last_request.text
        assert "grant_type=password" in sent
        assert "auth_chain=OAuthLdapService" in sent
        assert "username=svc_user" in sent


def test_token_cached_until_expiry():
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        c = _client()
        c.get_token()
        c.get_token()
        assert m.call_count == 1  # second call served from cache


def test_post_json_raises_payload_too_large_on_413():
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(f"{HOST}/api/v1/commons/data/multi_read", status_code=413, json={"error": "too big"})
        c = _client()
        with pytest.raises(PayloadTooLargeError):
            c.post_json("/commons/data/multi_read", {"select": []})


def test_post_json_401_triggers_single_refresh_then_success():
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, [
            {"json": {"access_token": "T1", "refresh_token": "R1", "expires_in": 3600}},
            {"json": {"access_token": "T2", "refresh_token": "R2", "expires_in": 3600}},
        ])
        m.post(f"{HOST}/api/v1/x", [
            {"status_code": 401, "json": {}},
            {"status_code": 200, "json": {"ok": True}},
        ])
        c = _client()
        assert c.post_json("/x", {}) == {"ok": True}


def test_exhausted_retries_surface_as_user_exception(monkeypatch):
    # A persistent 5xx exhausts the backoff retries and must surface as a UserException
    # (exit 1), never a bare HTTPError (exit 2).
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(f"{HOST}/api/v1/x", status_code=500, json={"error": "boom"})
        c = _client()
        with pytest.raises(UserException):
            c.post_json("/x", {})


def test_connection_error_surfaces_as_user_exception(monkeypatch):
    # A transient network error must be retried then surface as a UserException, not escape raw.
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(f"{HOST}/api/v1/x", exc=requests.ConnectionError("network down"))
        c = _client()
        with pytest.raises(UserException):
            c.post_json("/x", {})

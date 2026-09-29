import time

import pytest
import requests
import requests_mock
from keboola.component.exceptions import UserException

from client.wfm_client import _ERROR_BODY_MAX_CHARS, PayloadTooLargeError, WfmClient, _error_detail

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
        m.post(
            AUTH_URL,
            [
                {"json": {"access_token": "T1", "refresh_token": "R1", "expires_in": 3600}},
                {"json": {"access_token": "T2", "refresh_token": "R2", "expires_in": 3600}},
            ],
        )
        m.post(
            f"{HOST}/api/v1/x",
            [
                {"status_code": 401, "json": {}},
                {"status_code": 200, "json": {"ok": True}},
            ],
        )
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


def test_non_numeric_expires_in_raises_user_exception():
    # A malformed 'expires_in' must be a clean UserException (exit 1), not an int() ValueError (exit 2).
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "expires_in": "not-a-number"})
        with pytest.raises(UserException):
            _client().get_token()


def test_short_ttl_does_not_apply_negative_safety_margin():
    # ttl below the safety margin must clamp at 0, not push the expiry into the past
    # (which would force a re-auth on every request — a tight refresh loop).
    from datetime import UTC, datetime, timedelta

    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "expires_in": 10})
        c = _client()
        t0 = datetime.now(UTC)
        c.get_token()
        assert c._token_expiry >= t0 - timedelta(seconds=1)


# --- CFTL-814: error-body detail surfaced in the job log (_error_detail / _call) ---------------


def test_error_detail_surfaces_error_code_and_message_in_user_exception():
    # {"errorCode","message"} is UKG's documented error-body shape; both must land in the message,
    # and the existing "UKG WFM API error on ..." prefix must stay intact.
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(
            f"{HOST}/api/v1/x",
            status_code=400,
            json={"errorCode": "WFP-90011", "message": "Unrecognized property"},
        )
        c = _client()
        with pytest.raises(UserException) as exc_info:
            c.post_json("/x", {})
    message = str(exc_info.value)
    assert message.startswith("UKG WFM API error on")
    assert "WFP-90011" in message
    assert "Unrecognized property" in message


def test_error_detail_surfaces_first_entry_from_errors_envelope():
    # A nested {"errors":[...]} envelope surfaces the FIRST entry's code/message only.
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(
            f"{HOST}/api/v1/x",
            status_code=400,
            json={"errors": [{"errorCode": "X", "message": "Y"}, {"errorCode": "Z", "message": "W"}]},
        )
        c = _client()
        with pytest.raises(UserException) as exc_info:
            c.post_json("/x", {})
    message = str(exc_info.value)
    assert "X" in message
    assert "Y" in message
    assert "Z" not in message


def test_error_detail_surfaces_first_entry_from_bare_list_envelope():
    # A bare list body [{"errorCode","message"}, ...] is also tolerated (first entry wins).
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(f"{HOST}/api/v1/x", status_code=400, json=[{"errorCode": "X", "message": "Y"}])
        c = _client()
        with pytest.raises(UserException) as exc_info:
            c.post_json("/x", {})
    message = str(exc_info.value)
    assert "X" in message
    assert "Y" in message


def test_error_detail_falls_back_to_raw_text_for_non_json_body():
    # A non-JSON (e.g. HTML) error body still surfaces something useful, never crashes.
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(f"{HOST}/api/v1/x", status_code=400, text="<html><body>Bad Request</body></html>")
        c = _client()
        with pytest.raises(UserException) as exc_info:
            c.post_json("/x", {})
    assert "Bad Request" in str(exc_info.value)


def test_error_detail_empty_body_has_no_dangling_separator():
    # An empty error body must leave the original prefix untouched -- no trailing " — " appended.
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(f"{HOST}/api/v1/x", status_code=400, reason="Bad Request", text="")
        c = _client()
        with pytest.raises(UserException) as exc_info:
            c.post_json("/x", {})
    message = str(exc_info.value)
    assert message == "UKG WFM API error on /api/v1/x: HTTP 400 Bad Request"
    assert "—" not in message


def test_error_detail_truncates_overlong_body():
    # A body far exceeding the cap must be clipped to _ERROR_BODY_MAX_CHARS (+ the "…" marker).
    long_message = "x" * 600
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(f"{HOST}/api/v1/x", status_code=400, json={"errorCode": "E1", "message": long_message})
        c = _client()
        with pytest.raises(UserException) as exc_info:
            c.post_json("/x", {})
    detail = str(exc_info.value).split(" — ", 1)[1]
    assert len(detail) <= _ERROR_BODY_MAX_CHARS + 1  # +1 for the trailing "…" truncation marker
    assert len(detail) < len(long_message)


def test_error_detail_collapses_embedded_whitespace_to_single_spaces():
    # Embedded newlines/repeated whitespace in the error body must be collapsed to single spaces
    # so the job log stays one readable line, not a multi-line dump.
    with requests_mock.Mocker() as m:
        m.post(AUTH_URL, json={"access_token": "T1", "refresh_token": "R1", "expires_in": 3600})
        m.post(
            f"{HOST}/api/v1/x",
            status_code=400,
            json={"errorCode": "E1", "message": "line one\n\n  line   two\ttabbed"},
        )
        c = _client()
        with pytest.raises(UserException) as exc_info:
            c.post_json("/x", {})
    message = str(exc_info.value)
    assert "\n" not in message
    assert "\t" not in message
    assert "  " not in message
    assert "line one line two tabbed" in message


def test_error_detail_swallows_unexpected_failure_and_returns_empty():
    # If accessing the response body itself blows up, _error_detail must never propagate -- it
    # must return "" so the original HTTPError-derived UserException surfaces undecorated.
    class _BoomResponse:
        @property
        def text(self):
            raise RuntimeError("boom")

    assert _error_detail(_BoomResponse()) == ""

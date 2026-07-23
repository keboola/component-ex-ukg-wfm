import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import backoff
import requests
from keboola.component.exceptions import UserException

_TOKEN_SAFETY_MARGIN_S = 60
_RETRIABLE_STATUS = frozenset({408, 429})
_BACKOFF_MIN_WAIT_S = 1.0


def _endpoint(url: str) -> str:
    """Path (+query) of a full URL — a host-agnostic locator for error logs.

    The tenant host differs between a live recording (real host, sanitized to a placeholder in
    captured logs) and VCR replay (placeholder host), so logging the full URL makes failure-test
    log comparisons diverge on the host alone. The path identifies the endpoint unambiguously and
    is identical in both, and the operator already knows their own tenant host.
    """
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


class PayloadTooLargeError(Exception):
    """Raised on HTTP 413 so the engine can shrink the employee chunk and retry."""


def _is_retriable(e: Exception) -> bool:
    # Transient network faults are always worth retrying.
    if isinstance(e, requests.ConnectionError | requests.Timeout):
        return True
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        return code in _RETRIABLE_STATUS or code >= 500
    return False


class WfmClient:
    def __init__(self, host: str, client_id: str, client_secret: str, username: str, password: str):
        host = host.rstrip("/")
        if not host.startswith("https://"):
            raise UserException("Host must be an absolute https:// tenant URL.")
        self._auth_url = f"{host}/api/authentication/access_token"
        self._api_base = f"{host}/api/v1"
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: datetime | None = None

    def get_token(self) -> str:
        now = datetime.now(UTC)
        if self._token and self._token_expiry and now < self._token_expiry:
            return self._token
        if self._refresh_token:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        else:
            data = {
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "auth_chain": "OAuthLdapService",
            }
        try:
            resp = requests.post(
                self._auth_url,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as e:
            # A failed refresh falls back to a full password grant on the next call.
            self._refresh_token = None
            raise UserException(f"Failed to obtain UKG WFM token: {type(e).__name__}") from e
        except ValueError as e:
            raise UserException(f"UKG WFM token response malformed: {e}") from e
        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not access_token or expires_in is None:
            raise UserException("UKG WFM token response missing 'access_token'/'expires_in'.")
        try:
            ttl_s = int(expires_in)
        except (TypeError, ValueError) as e:
            raise UserException(f"UKG WFM token 'expires_in' is not numeric: {expires_in!r}") from e
        self._token = access_token
        self._refresh_token = body.get("refresh_token") or self._refresh_token
        # Clamp at 0 so a short-lived token (ttl < safety margin) doesn't land the expiry in the
        # past and force a re-auth on every single request (tight refresh loop).
        self._token_expiry = now + timedelta(seconds=max(ttl_s - _TOKEN_SAFETY_MARGIN_S, 0))
        logging.info("Obtained UKG WFM OAuth token (expires in %ss).", expires_in)
        return self._token

    @property
    def api_base(self) -> str:
        """Public base URL (``https://<host>/api/v1``) for callers building raw download URLs."""
        return self._api_base

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("POST", f"{self._api_base}{path}", json_body=body)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._call("GET", f"{self._api_base}{path}", params=params)

    def _call(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        # Retry exhaustion re-raises the final HTTPError, and transient ConnectionError/Timeout
        # propagate once the retriable set gives up — convert both to UserException (exit 1),
        # mirroring get_token(), so neither escapes as a bare exception (exit 2).
        try:
            resp = self._request_with_retry(method, url, json_body, params)
            if resp.status_code == 401:
                self._token = None
                self._token_expiry = None
                resp = self._request_with_retry(method, url, json_body, params)
        except requests.RequestException as e:
            raise UserException(f"UKG WFM request to {_endpoint(url)} failed: {type(e).__name__}") from e
        if resp.status_code == 413:
            raise PayloadTooLargeError(url)
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            reason = e.response.reason if e.response is not None else ""
            raise UserException(f"UKG WFM API error on {_endpoint(url)}: HTTP {status} {reason}") from e
        try:
            return resp.json()
        except ValueError as e:
            raise UserException(f"UKG WFM returned non-JSON response for {_endpoint(url)}") from e

    @backoff.on_exception(
        backoff.expo,
        requests.RequestException,
        max_tries=5,
        factor=_BACKOFF_MIN_WAIT_S,
        giveup=lambda e: not _is_retriable(e),
        on_backoff=lambda details: _honor_retry_after(details),
    )
    def _request_with_retry(
        self, method: str, url: str, json_body: dict[str, Any] | None, params: dict[str, Any] | None
    ) -> requests.Response:
        resp = requests.request(
            method,
            url,
            json=json_body,
            params=params,
            headers={"Authorization": f"Bearer {self.get_token()}", "Accept": "application/json"},
            timeout=120,
        )
        if resp.status_code in _RETRIABLE_STATUS or resp.status_code >= 500:
            resp.raise_for_status()
        return resp

    def request_raw(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {self.get_token()}")
        return requests.request(method, url, headers=headers, timeout=300, **kwargs)


def _honor_retry_after(details: dict[str, Any]) -> None:
    exc = details.get("exception")
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                wait = float(retry_after) - details.get("wait", 0)
            except ValueError:
                return
            if wait > 0:
                time.sleep(wait)

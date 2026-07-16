import csv
import logging
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import requests
from keboola.component.exceptions import UserException

from client.resources import ResourceDef
from client.wfm_client import WfmClient

_DOWNLOAD_CHUNK_BYTES = 1 << 16

# VERIFIED live: the async payroll export reports state=SUCCESSFUL on completion.
_TERMINAL_OK = {"SUCCESSFUL", "COMPLETED", "SUCCEEDED", "DONE"}
_TERMINAL_FAIL = {"FAILED", "CANCELLED", "CANCELED", "ERROR", "EXPIRED"}


def run_async_export(
    client: WfmClient,
    resource: ResourceDef,
    since_iso: str,
    until_iso: str,
    hyperfind_ref: str | None,
    *,
    query: str | None = None,
    max_wait_s: int = 1800,
    poll_interval_s: int = 15,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict[str, Any]]:
    """Run the UKG WFM async payroll export end to end and yield the CSV rows.

    VERIFIED live flow:
      1. submit  POST /commons/payroll/export/async  {query, requestId, ...} -> 202
                 {"executionKey","state","message","nextPing","expiresAt"}
      2. poll    GET  /commons/payroll/export/async  -> {"records":[{executionKey,state,...}]};
                 wait for our executionKey to reach state=SUCCESSFUL
      3. fetch   GET  /commons/payroll/export/async/{executionKey}/response  -> CSV
    """
    if not query:
        raise UserException(
            "Resource 'payroll_export' requires a tenant-defined query. Set config 'payroll_query'."
        )
    submit_body: dict[str, Any] = dict(resource.body_template)
    submit_body["query"] = query
    submit_body["requestId"] = f"kbc-{uuid.uuid4().hex}"
    submitted = client.post_json(resource.endpoint_path, submit_body)
    execution_key = submitted.get("executionKey") if isinstance(submitted, dict) else None
    if not execution_key:
        raise UserException("Payroll export submit did not return an executionKey.")
    state = (submitted.get("state") or "").upper() if isinstance(submitted, dict) else ""

    if state not in _TERMINAL_OK:
        state = _poll_until_terminal(client, resource, execution_key, max_wait_s, poll_interval_s, sleep)

    response_path = f"{resource.endpoint_path}/{execution_key}/response"
    yield from _download_and_parse_csv(client, f"{client.api_base}{response_path}", execution_key)


def _poll_until_terminal(
    client: WfmClient,
    resource: ResourceDef,
    execution_key: str,
    max_wait_s: int,
    poll_interval_s: int,
    sleep: Callable[[float], None],
) -> str:
    """Poll the async-export list until our executionKey reaches a terminal state; return it."""
    waited = 0
    while True:
        listing = client.get_json(resource.endpoint_path)
        records = listing.get("records", []) if isinstance(listing, dict) else []
        state = ""
        for rec in records:
            if isinstance(rec, dict) and rec.get("executionKey") == execution_key:
                state = (rec.get("state") or "").upper()
                break
        if state in _TERMINAL_OK:
            return state
        if state in _TERMINAL_FAIL:
            raise UserException(f"Payroll export {execution_key} ended with state {state}.")
        if waited >= max_wait_s:
            raise UserException(
                f"Payroll export {execution_key} exceeded max wait of {max_wait_s}s (last state {state or 'UNKNOWN'})."
            )
        logging.debug("Payroll export %s state=%s; waiting %ss.", execution_key, state or "UNKNOWN", poll_interval_s)
        sleep(poll_interval_s)
        waited += poll_interval_s


def _download_and_parse_csv(
    client: WfmClient, download_url: str, execution_key: str
) -> Iterator[dict[str, Any]]:
    """Stream the CSV export body to a /tmp scratch file, then parse and yield rows as dicts.

    The raw HTTP body is streamed to disk in chunks (never buffered whole in RAM); the scratch file
    lives in /tmp (tempfile default), never under data/out/tables/. A transport failure surfaces as
    a UserException (exit 1) rather than escaping as a bare exception (exit 2).
    """
    try:
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", newline="", suffix=".csv") as tmp:
            resp = client.request_raw("GET", download_url, stream=True)
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES, decode_unicode=True):
                if chunk:
                    tmp.write(chunk)
            tmp.seek(0)
            reader = csv.DictReader(tmp)
            for row in reader:
                yield {k: v for k, v in row.items() if k is not None}
    except requests.RequestException as e:
        raise UserException(f"Payroll export {execution_key} download failed: {type(e).__name__}") from e

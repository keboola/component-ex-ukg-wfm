import json
import logging
import tempfile
import time
from collections.abc import Callable, Iterator
from typing import Any

import requests
from keboola.component.exceptions import UserException

from client.orchestration import extract_records
from client.resources import ResourceDef
from client.wfm_client import WfmClient

_DOWNLOAD_CHUNK_BYTES = 1 << 16

_TERMINAL_OK = {"COMPLETED", "SUCCEEDED", "DONE"}
_TERMINAL_FAIL = {"FAILED", "CANCELLED", "ERROR"}


def run_async_export(
    client: WfmClient,
    resource: ResourceDef,
    since_iso: str,
    until_iso: str,
    hyperfind_ref: str | None,
    *,
    max_wait_s: int = 1800,
    poll_interval_s: int = 15,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict]:
    submit_body: dict[str, Any] = dict(resource.body_template)
    if resource.date_field:
        submit_body["dateRange"] = {"startDate": since_iso, "endDate": until_iso}
    if hyperfind_ref:
        submit_body["hyperfind"] = {"id": hyperfind_ref}
    submitted = client.post_json(resource.endpoint_path, submit_body)
    job_id = submitted.get("id") if isinstance(submitted, dict) else None
    if not job_id:
        raise UserException("Payroll export submit did not return a job id.")

    waited = 0
    download_url: str | None = None
    while True:
        status_body = client.get_json(f"{resource.endpoint_path}/{job_id}/status")
        status = (status_body.get("status") or "").upper()
        if status in _TERMINAL_OK:
            download_url = status_body.get("downloadUrl")
            break
        if status in _TERMINAL_FAIL:
            raise UserException(f"Payroll export job {job_id} ended with status {status}.")
        if waited >= max_wait_s:
            raise UserException(
                f"Payroll export job {job_id} exceeded max wait of {max_wait_s}s (last status {status})."
            )
        logging.debug("Payroll export %s status=%s; waiting %ss.", job_id, status, poll_interval_s)
        sleep(poll_interval_s)
        waited += poll_interval_s

    if not download_url:
        download_url = f"{client.api_base}{resource.endpoint_path}/{job_id}/file"
    yield from _download_and_parse(client, download_url, job_id)


def _download_and_parse(client: WfmClient, download_url: str, job_id: str) -> Iterator[dict[str, Any]]:
    """Stream the export body to a /tmp scratch file, then parse and yield rows.

    The raw HTTP body is streamed to disk in chunks (never buffered whole in RAM); the
    scratch file lives in /tmp (tempfile default), never under data/out/tables/. Both the
    download and the parse are wrapped so a transport or malformed-body failure surfaces as
    a UserException (exit 1) rather than escaping as a bare exception (exit 2).
    """
    try:
        with tempfile.NamedTemporaryFile(mode="w+b", suffix=".json") as tmp:
            resp = client.request_raw("GET", download_url, stream=True)
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                if chunk:
                    tmp.write(chunk)
            tmp.seek(0)
            payload = json.load(tmp)
    except (requests.RequestException, ValueError) as e:
        raise UserException(
            f"Payroll export {job_id} download/parse failed: {type(e).__name__}"
        ) from e
    yield from extract_records(payload)

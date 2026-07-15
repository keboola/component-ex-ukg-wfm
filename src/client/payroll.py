import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

from keboola.component.exceptions import UserException

from client.orchestration import extract_records
from client.resources import ResourceDef
from client.wfm_client import WfmClient

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
        logging.info("Payroll export %s status=%s; waiting %ss.", job_id, status, poll_interval_s)
        sleep(poll_interval_s)
        waited += poll_interval_s

    if not download_url:
        download_url = f"{client.api_base}{resource.endpoint_path}/{job_id}/file"
    resp = client.request_raw("GET", download_url)
    resp.raise_for_status()
    yield from extract_records(resp.json())

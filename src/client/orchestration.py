import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from client.resources import EmployeeScope, HttpMethod, IncrementalStyle, PaginationStyle, ResourceDef
from client.wfm_client import PayloadTooLargeError, WfmClient
from client.window import split_date_windows

PAGE_SIZE = 500
_MAX_PAGES = 100_000


def resolve_employee_ids(client: WfmClient, hyperfind_ref: str | None) -> list[int]:
    body: dict[str, Any] = (
        {"hyperfind": {"id": hyperfind_ref}}
        if hyperfind_ref
        else {"hyperfind": {"qualifier": "All Home"}}
    )
    result = client.post_json("/commons/hyperfind/execute", body)
    rows = result.get("result", []) if isinstance(result, dict) else []
    return [r["id"] for r in rows if "id" in r]


def paginate_multi_read(client: WfmClient, resource: ResourceDef, body: dict) -> Iterator[dict]:
    if resource.pagination != PaginationStyle.MULTI_READ:
        if resource.method == HttpMethod.GET:
            result = client.get_json(resource.endpoint_path)
        else:
            result = client.post_json(resource.endpoint_path, body)
        yield from extract_records(result)
        return
    index = 0
    pages = 0
    cache_key: str | None = None
    while pages < _MAX_PAGES:
        pages += 1
        page_body = dict(body)
        page_body.setdefault("count", PAGE_SIZE)
        if cache_key is not None:
            page_body["cacheKey"] = cache_key
            page_body["index"] = index
        result = client.post_json(resource.endpoint_path, page_body)
        records = extract_records(result)
        yield from records
        count = page_body["count"]
        cache_key = result.get("cacheKey") if isinstance(result, dict) else None
        index += len(records)
        if cache_key is None or len(records) < count:
            return


def chunk_and_read(
    client: WfmClient,
    resource: ResourceDef,
    emp_ids: list[int],
    since_iso: str,
    until_iso: str,
    select: list[str],
    symbolic_period: str | None = None,
) -> Iterator[dict]:
    chunk_size = resource.batch_limit or len(emp_ids) or 1
    chunks = _initial_chunks(emp_ids, chunk_size) if emp_ids else [[]]
    for chunk in chunks:
        yield from _read_chunk_with_shrink(
            client, resource, chunk, since_iso, until_iso, select, symbolic_period
        )


def _read_chunk_with_shrink(
    client: WfmClient, resource: ResourceDef, chunk: list[int],
    since_iso: str, until_iso: str, select: list[str], symbolic_period: str | None = None,
) -> Iterator[dict]:
    size = len(chunk) or 1
    while True:
        try:
            body = _build_body(resource, chunk, since_iso, until_iso, select, symbolic_period)
            yield from paginate_multi_read(client, resource, body)
            return
        except PayloadTooLargeError:
            if size <= 1:
                raise
            size = max(1, size // 2)
            logging.warning("413 on %s; shrinking chunk to %s employees.", resource.name, size)
            # Re-run the sub-chunks at the smaller size.
            for sub in _initial_chunks(chunk, size):
                yield from _read_chunk_with_shrink(
                    client, resource, sub, since_iso, until_iso, select, symbolic_period
                )
            return


def _initial_chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)] or [[]]


def _build_body(
    resource: ResourceDef, chunk: list[int], since_iso: str, until_iso: str, select: list[str],
    symbolic_period: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = dict(resource.body_template)
    if select or resource.select:
        body["select"] = select or resource.select
    if chunk:
        body["where"] = {"employees": {"ids": chunk}}
    if symbolic_period:
        # Symbolic period (e.g. "Current Pay Period") replaces the explicit date window.
        body.setdefault("where", {})["symbolicPeriod"] = {"qualifier": symbolic_period}
    elif resource.date_field and since_iso and until_iso:
        body.setdefault("where", {})["dateRange"] = {"startDate": since_iso, "endDate": until_iso}
    return body


def extract_records(result: Any) -> list[dict]:
    if isinstance(result, dict):
        for key in ("records", "result", "data"):
            if isinstance(result.get(key), list):
                return result[key]
        return [result]
    if isinstance(result, list):
        return result
    return []


_WINDOWED_STYLES = (IncrementalStyle.DATE_WINDOW, IncrementalStyle.NET_CHANGE)


def iter_records(
    client: WfmClient,
    resource: ResourceDef,
    *,
    hyperfind_ref: str | None,
    since_iso: str | None,
    until_iso: str | None,
    select: list[str],
    symbolic_period: str | None = None,
) -> Iterator[dict]:
    emp_ids: list[int] = []
    if resource.employee_scope == EmployeeScope.HYPERFIND:
        emp_ids = resolve_employee_ids(client, hyperfind_ref)

    # A symbolic period (e.g. "Current Pay Period") replaces the date window entirely; the
    # caller has already skipped window/watermark logic, so read once with the symbolic bound.
    if symbolic_period:
        yield from chunk_and_read(client, resource, emp_ids, "", "", select, symbolic_period)
        return

    # NET_CHANGE resources are treated as a plain date window (case-2 full refresh): they
    # have no stable PK to upsert against, so a real net-change delta token would only
    # produce duplicate/unmergeable rows. True delta requires a stable PK + persisted token
    # (see the comment on work_activity_net_changes in resources.py). Until then, both
    # DATE_WINDOW and NET_CHANGE just read [since, now] and full-REPLACE.
    if resource.incremental_style in _WINDOWED_STYLES and since_iso and until_iso:
        start = datetime.fromisoformat(since_iso)
        end = datetime.fromisoformat(until_iso)
        for w_start, w_end in split_date_windows(start, end):
            yield from chunk_and_read(
                client, resource, emp_ids, w_start.isoformat(), w_end.isoformat(), select
            )
    else:
        yield from chunk_and_read(client, resource, emp_ids, since_iso or "", until_iso or "", select)

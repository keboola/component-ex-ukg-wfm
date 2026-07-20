import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from keboola.component.exceptions import UserException

from client.resources import BodyStyle, EmployeeScope, HttpMethod, IncrementalStyle, PaginationStyle, ResourceDef
from client.wfm_client import PayloadTooLargeError, WfmClient
from client.window import split_date_windows


def _as_date(iso: str | None) -> str | None:
    """WFM read endpoints want a plain calendar date (YYYY-MM-DD); a full ISO datetime is
    rejected with WFP-90100 / TKException 'Invalid Parameter Date String'. Truncate to 10 chars."""
    return iso[:10] if iso else None


def _as_datetime(iso: str | None) -> str | None:
    """WFM apply_read/net-change endpoints want a tz-naive datetime (YYYY-MM-DDTHH:MM:SS).
    Strip any timezone suffix / microseconds by keeping the first 19 chars."""
    return iso[:19] if iso else None


def resolve_employee_ids(
    client: WfmClient,
    hyperfind_ref: str | None,
    since_iso: str | None = None,
    until_iso: str | None = None,
) -> list[int]:
    """Execute a Hyperfind and return its employee IDs (from result.refs[].id).

    hyperfind/execute REQUIRES a dateRange and returns {"count", "result": {"refs":[{id,qualifier}],
    "basePersons":[...]}}. A too-large Hyperfind (> tenant threshold, e.g. "All Home" = 11k) is
    rejected server-side (WCO-112003) and surfaces as a UserException — pick a narrower Hyperfind.
    """
    ref: dict[str, Any] = {"qualifier": "All Home"}
    if hyperfind_ref:
        ref = {"id": int(hyperfind_ref)} if str(hyperfind_ref).lstrip("-").isdigit() else {"qualifier": hyperfind_ref}
    start = _as_date(since_iso) or _as_date(until_iso) or datetime.now().strftime("%Y-%m-%d")
    end = _as_date(until_iso) or start
    body: dict[str, Any] = {"hyperfind": ref, "dateRange": {"startDate": start, "endDate": end}}
    result = client.post_json("/commons/hyperfind/execute", body)
    if not isinstance(result, dict):
        return []
    inner = result.get("result")
    # Real API nests refs under result.refs; tolerate a bare list for legacy/mocked shapes.
    rows = inner.get("refs", []) if isinstance(inner, dict) else (inner if isinstance(inner, list) else [])
    return [r["id"] for r in rows if isinstance(r, dict) and "id" in r]


_APPLY_READ_MAX_PAGES = 100_000  # safety cap so a misbehaving endpoint can't loop forever


def paginate_multi_read(client: WfmClient, resource: ResourceDef, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Issue the read(s) for the given (already employee-batched) body and yield its records.

    multi_read endpoints return the full result set for the employee batch in a single response —
    they do NOT accept the commons cacheKey/count/index paging params (those return WFP-90011).
    Volume is bounded instead by employee batching in chunk_and_read, so that is one request per
    chunk. apply_read endpoints (persons, punches) page via count+index in the request body.
    """
    if resource.pagination == PaginationStyle.APPLY_READ:
        yield from _paginate_apply_read(client, resource, body)
        return
    if resource.method == HttpMethod.GET:
        result = client.get_json(resource.endpoint_path)
    else:
        result = client.post_json(resource.endpoint_path, body)
    yield from extract_records(result, resource.records_key)


def _paginate_apply_read(
    client: WfmClient, resource: ResourceDef, body: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Page an apply_read endpoint via count+index in the request body.

    Loop incrementing the 0-based page `index`, injecting `count` (page size), until a page returns
    fewer than `count` records. `body` already carries the where clause from _build_body.
    """
    count = resource.page_count or 100
    for index in range(_APPLY_READ_MAX_PAGES):
        page_body = {**body, "index": index, "count": count}
        result = client.post_json(resource.endpoint_path, page_body)
        records = extract_records(result, resource.records_key)
        yield from records
        if len(records) < count:
            return


def chunk_and_read(
    client: WfmClient,
    resource: ResourceDef,
    emp_ids: list[int],
    since_iso: str,
    until_iso: str,
    select: list[str],
    symbolic_period: str | None = None,
    hyperfind_ref: str | None = None,
) -> Iterator[dict[str, Any]]:
    chunk_size = resource.batch_limit or len(emp_ids) or 1
    chunks = _initial_chunks(emp_ids, chunk_size) if emp_ids else [[]]
    for chunk in chunks:
        yield from _read_chunk_with_shrink(
            client, resource, chunk, since_iso, until_iso, select, symbolic_period, hyperfind_ref
        )


def _read_chunk_with_shrink(
    client: WfmClient,
    resource: ResourceDef,
    chunk: list[int],
    since_iso: str,
    until_iso: str,
    select: list[str],
    symbolic_period: str | None = None,
    hyperfind_ref: str | None = None,
) -> Iterator[dict[str, Any]]:
    size = len(chunk) or 1
    while True:
        try:
            body = _build_body(resource, chunk, since_iso, until_iso, select, symbolic_period, hyperfind_ref)
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
                    client, resource, sub, since_iso, until_iso, select, symbolic_period, hyperfind_ref
                )
            return


def _initial_chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)] or [[]]


def _date_range(since_iso: str, until_iso: str) -> dict[str, str] | None:
    """Build a WFM {startDate,endDate} calendar-date range, or None if bounds are missing."""
    start, end = _as_date(since_iso), _as_date(until_iso)
    return {"startDate": start, "endDate": end} if start and end else None


def _build_body(
    resource: ResourceDef,
    chunk: list[int],
    since_iso: str,
    until_iso: str,
    select: list[str],
    symbolic_period: str | None = None,
    hyperfind_ref: str | None = None,
) -> dict[str, Any]:
    """Assemble the request body in the shape the endpoint's family expects (see BodyStyle)."""
    style = resource.body_style
    date_range = _date_range(since_iso, until_iso)
    sel = select or resource.select

    if style == BodyStyle.STATIC:
        # Body emitted verbatim (org-level fixed criteria, e.g. generic_locations).
        return dict(resource.body_template)

    if style == BodyStyle.APPLY_READ_PERSONS:
        # Org-wide bulk read; count/index are injected by the apply_read paginator.
        return dict(resource.body_template)

    if style == BodyStyle.APPLY_READ_PUNCHES:
        # where.employees.ids + where.dateRange with DATETIME keys; count/index added by paginator.
        where: dict[str, Any] = {}
        if chunk:
            where["employees"] = {"ids": chunk}
        start_dt, end_dt = _as_datetime(since_iso), _as_datetime(until_iso)
        if start_dt and end_dt:
            where["dateRange"] = {"startDateTime": start_dt, "endDateTime": end_dt}
        return {"where": where}

    if style == BodyStyle.EMPLOYEE_SET_METRICS:
        # timecard_metrics/multi_read: the selector field is "select" (an array); the accruals
        # resources ride this endpoint with select=["ACCRUAL_SUMMARY"|"ACCRUAL_TRANSACTIONS"].
        # An absent select makes the API return all resource rollups (the timecard_metrics default).
        body = dict(resource.body_template)
        if sel:
            body["select"] = sel
        employee_set: dict[str, Any] = {}
        if chunk:
            employee_set["employees"] = {"ids": chunk}
        if symbolic_period:
            employee_set["symbolicPeriod"] = {"qualifier": symbolic_period}
        elif date_range:
            employee_set["dateRange"] = date_range
        body["where"] = {"employeeSet": employee_set}
        return body

    if style == BodyStyle.SWAP_EMPLOYEES:
        # where.employees is a criterion object: employees is an ARRAY of refs, start/end dates sit
        # alongside it (NOT under where.dateRange). VERIFIED 200 live for employee_swap/multi_read.
        emp_criterion: dict[str, Any] = {}
        if chunk:
            emp_criterion["employees"] = [{"id": emp_id} for emp_id in chunk]
        if symbolic_period:
            emp_criterion["symbolicPeriod"] = {"qualifier": symbolic_period}
        elif date_range:
            emp_criterion["startDate"] = date_range["startDate"]
            emp_criterion["endDate"] = date_range["endDate"]
        return {"where": {"employees": emp_criterion}}

    if style == BodyStyle.LOCATIONS_QUERY:
        # Legacy /commons/locations/multi_read: where.query is an org-map keyword search requiring a
        # context ("ORG"), a snapshot date, and a query string q. date defaults to the run's upper
        # bound (now). VERIFIED 200 live. body_template.query may override context/q.
        snapshot = _as_date(until_iso) or datetime.now().strftime("%Y-%m-%d")
        query: dict[str, Any] = {"context": "ORG", "q": "/"}
        query.update(resource.body_template.get("query", {}))
        query["date"] = snapshot
        return {"where": {"query": query}}

    if style == BodyStyle.WHERE_EMPLOYEES_LIST:
        body = dict(resource.body_template)
        where = {}
        if chunk:
            where["employees"] = [{"id": emp_id} for emp_id in chunk]
        if symbolic_period:
            where["symbolicPeriod"] = {"qualifier": symbolic_period}
        elif date_range:
            where["dateRange"] = date_range
        body["where"] = where
        return body

    if style == BodyStyle.NET_CHANGE_SEGMENTS:
        # where.employees.ids + lastRunDateTime/endDateTime (DATETIME); select from body_template.
        body = dict(resource.body_template)
        where = {}
        if chunk:
            where["employees"] = {"ids": chunk}
        start_dt, end_dt = _as_datetime(since_iso), _as_datetime(until_iso)
        if start_dt and end_dt:
            where["lastRunDateTime"] = start_dt
            where["endDateTime"] = end_dt
        body["where"] = where
        return body

    body: dict[str, Any] = dict(resource.body_template)
    if sel:
        body["select"] = sel

    if style == BodyStyle.WHERE_EMPLOYEE_REFS:
        employees: dict[str, Any] = {}
        if chunk:
            employees["employeeRefs"] = {"ids": chunk}
        if symbolic_period:
            employees["symbolicPeriod"] = {"qualifier": symbolic_period}
        elif date_range:
            employees["startDate"] = date_range["startDate"]
            employees["endDate"] = date_range["endDate"]
        body["where"] = {"employees": employees}
        return body

    if style == BodyStyle.TOP_EMPLOYEES_IDS:
        if chunk:
            body["employees"] = {"ids": chunk}
        if symbolic_period:
            body["symbolicPeriod"] = {"qualifier": symbolic_period}
        elif date_range:
            body["dateRange"] = date_range
        return body

    # Default: BodyStyle.WHERE_EMPLOYEES_IDS
    if chunk:
        body["where"] = {"employees": {"ids": chunk}}
    if symbolic_period:
        body.setdefault("where", {})["symbolicPeriod"] = {"qualifier": symbolic_period}
    elif date_range:
        body.setdefault("where", {})["dateRange"] = date_range
    return body


def extract_records(result: Any, records_key: str | None = None) -> list[dict[str, Any]]:
    """Pull the record list out of a response, honoring the resource's declared envelope key.

    records_key set  -> return result[key] (a dict value is wrapped as a single-row list).
    records_key None -> a root-level list, else the legacy ("records","result","data") fallback.
    """
    if records_key is not None and isinstance(result, dict):
        val = result.get(records_key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            return [val]
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("records", "result", "data"):
            if isinstance(result.get(key), list):
                return result[key]
        return [result]
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
) -> Iterator[dict[str, Any]]:
    emp_ids: list[int] = []
    if resource.employee_scope == EmployeeScope.HYPERFIND:
        emp_ids = resolve_employee_ids(client, hyperfind_ref, since_iso, until_iso)
        # An empty Hyperfind result means "no matching employees". Without this guard the
        # request would ship with no where.employees.ids, which WFM treats as ALL employees
        # — a huge, wrongly-scoped read. Org-level resources (employee_scope != HYPERFIND)
        # legitimately omit the employee filter, so this short-circuit is HYPERFIND-only.
        if not emp_ids:
            return
    elif resource.employee_scope == EmployeeScope.HYPERFIND_SERVER and not hyperfind_ref:
        # Server-side scoping (Information Access) needs a Hyperfind reference; without one the
        # only fallback ("All Home") exceeds the tenant threshold. Fail closed rather than
        # issuing an unbounded query.
        raise UserException(
            f"Resource '{resource.name}' requires a hyperfind_ref (server-side Hyperfind scoping)."
        )

    # A symbolic period (e.g. "Current Pay Period") replaces the date window entirely; the
    # caller has already skipped window/watermark logic, so read once with the symbolic bound.
    if symbolic_period:
        yield from chunk_and_read(client, resource, emp_ids, "", "", select, symbolic_period, hyperfind_ref)
        return

    # NET_CHANGE resources are treated as a plain date window (case-2 full refresh): they
    # have no stable PK to upsert against, so a real net-change delta token would only
    # produce duplicate/unmergeable rows. True delta requires a stable PK + persisted token
    # (see the comment on work_activity_net_changes in resources.py). Until then, both
    # DATE_WINDOW and NET_CHANGE just read [since, now] and full-REPLACE.
    if resource.incremental_style in _WINDOWED_STYLES and since_iso and until_iso:
        start = datetime.fromisoformat(since_iso)
        end = datetime.fromisoformat(until_iso)
        # Sub-hour granularity for endpoints with a per-call window cap (punches <= 60 min).
        for w_start, w_end in split_date_windows(start, end, max_minutes=resource.window_max_minutes):
            yield from chunk_and_read(
                client, resource, emp_ids, w_start.isoformat(), w_end.isoformat(), select, None, hyperfind_ref
            )
    else:
        yield from chunk_and_read(
            client, resource, emp_ids, since_iso or "", until_iso or "", select, None, hyperfind_ref
        )
